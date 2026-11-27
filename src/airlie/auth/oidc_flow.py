"""The OIDC authorization-code flow: an attempt in flight, and the routes that open and close one.

Split from `auth/oidc.py` on purpose. That module is pure verification — token and key ring in,
claims or a refusal out — and it reads no clock and fetches nothing, which is what makes every
rejection testable. This module holds the stateful half: a store with a clock, and HTTP. Keeping
them apart is what lets the verification stay honest.

The flow (docs/superpowers/specs/2026-11-26-oidc-sign-in-design.md):

    /start   makes state, nonce and a PKCE verifier, saves them, and sends the browser to the IdP
    /callback matches and consumes the state, exchanges the code with the verifier, verifies the ID
             token against the saved nonce, applies airlie's gates, and mints the session a passkey
             login would get

A callback that ends in a session is indistinguishable from a passkey login to everything
downstream: `Resolver.resolve()` never learns which sign-in produced the cookie."""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

#: How long a pending attempt lives. A person mid-redirect is typing nothing, so minutes are
#: enough, and it bounds how long a leaked state is worth replaying.
ATTEMPT_TTL_SECONDS = 300.0

#: `token_urlsafe(48)` is 64 characters of the base64url alphabet: inside RFC 7636's 43-128 for
#: the verifier, and long enough that state and nonce are unguessable rather than merely uncommon.
SECRET_BYTES = 48


def code_challenge(verifier: str) -> str:
    """BASE64URL(SHA256(verifier)) without padding, which is what `method=S256` means.

    Padding or standard base64 yields a challenge the IdP recomputes to something else, and the
    failure surfaces at the token exchange as `invalid_request` naming nothing useful."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_attempt(*, redirect_uri: str, now: float, ttl_seconds: float = ATTEMPT_TTL_SECONDS) -> Pending:
    """The three secrets one browser interaction gets, none derived from anything it offered.

    They are distinct because they answer different attacks. `state` says this callback answers a
    request this server made (CSRF). `nonce` says this ID token was minted for this attempt rather
    presented from elsewhere (token injection). The verifier says the party exchanging the code is
    the party that started it (code interception). Reusing one value across two roles gives an
    attacker both for the price of one."""
    return Pending(
        state=secrets.token_urlsafe(SECRET_BYTES),
        nonce=secrets.token_urlsafe(SECRET_BYTES),
        verifier=secrets.token_urlsafe(SECRET_BYTES),
        redirect_uri=redirect_uri,
        expires_at=now + ttl_seconds,
    )


@dataclass(frozen=True)
class Pending:
    """One browser interaction that asked to sign in, and the three secrets that make its callback
    answerable only by that browser.

    There is deliberately no user here: airlie does not know who is signing in until an ID token
    has been verified, and an attempt that could pre-claim a person would let a callback complete
    as someone the browser never proved."""

    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    expires_at: float


#: The studio routes a sign-in may resume (`web/src/router.ts`). A target is allowed when it equals
#: one of these or begins with one followed by `/` — so `/boardadmin` is not `/board`. `/signin` is
#: listed so a call site can fall back to the sign-in screen rather than a guarded route that
#: would bounce the person back there anyway.
_STUDIO_ROUTES = (
    "/chat",
    "/workflows",
    "/board",
    "/metrics",
    "/admin",
    "/update",
    "/signin",
)
_MAX_TARGET = 2048


def _is_route(path: str) -> bool:
    """Whether `path` is one of the studio's own routes, query and fragment ignored."""
    head = path.partition("?")[0].partition("#")[0]
    return head == "/" or any(head == route or head.startswith(route + "/") for route in _STUDIO_ROUTES)


def _fallback(default: str) -> str:
    """`default` when it is itself a path we would take a person to, else the studio root.

    The guard lives here rather than at each call site because the whole value of `safe_target` is
    that its answer is always on-site. A caller that later passed a default built from a header
    would otherwise get an open redirect out of a function whose name promises the opposite."""
    return default if _is_route(default) else "/"


def safe_target(target: str | None, *, default: str) -> str:
    """The post-login destination, or `default` when the caller's value is not a path we would
    take a person to.

    /callback is public and its query is attacker-writable, so the destination is chosen from the
    route list rather than trusted because it starts with a slash. `//evil.net`, `/\\evil.net` and
    `/%2f%2fevil.net` all reach another origin in a browser — the last one only after a hop — so
    percent-escapes, backslashes and control characters disqualify a target outright instead of
    being decoded and re-checked. Decoding is where the mistake lives.

    A default is honoured only when it is one of the same routes, for the same reason: the answer
    must be on-site no matter which argument arrived."""
    if not target or len(target) > _MAX_TARGET or "%" in default:
        return _fallback(default)
    if "%" in target or ".." in target:
        return _fallback(default)
    if target != target.strip() or any(ch in target for ch in "\t\r\n\x00\\"):
        return _fallback(default)
    if not target.startswith("/") or target.startswith("//"):
        return _fallback(default)
    if target == "/":
        return "/"
    if _is_route(target):
        return target
    return _fallback(default)


class Attempts:
    """Authorization attempts in flight, in memory, keyed by `state`. One per serve process, like
    the WebAuthn challenges: a restart ends them and a person mid-redirect starts again.

    /start is a public route, so the store is bounded — an unbounded dict there is a way for a
    stranger to grow a serve process until it swaps."""

    def __init__(
        self, now: Callable[[], float], *, ttl_seconds: float = 300.0, max_pending: int = 1000
    ) -> None:
        self._now = now
        self._ttl = ttl_seconds
        self._max = max_pending
        self._by_state: dict[str, Pending] = {}
        self._order: deque[str] = deque()

    def __len__(self) -> int:
        return len(self._by_state)

    def open(self, attempt: Pending) -> None:
        """Save an attempt, evicting what has already expired and then the oldest if still at the
        cap."""
        self._sweep()
        self._by_state.pop(attempt.state, None)
        while len(self._by_state) >= self._max and self._order:
            self._by_state.pop(self._order.popleft(), None)
        self._by_state[attempt.state] = attempt
        self._order.append(attempt.state)

    def take(self, state: str) -> Pending | None:
        """The attempt this `state` answers, if it is still live — and it is gone either way.

        A code is single-use, so a second callback carrying the same state is a replay rather than
        a refresh, and handing the same verifier to the IdP twice would fail with a more confusing
        error than refusing."""
        found = self._by_state.pop(state, None)
        if found is not None:
            self._order.remove(state)
        if found is None or found.expires_at <= self._now():
            return None
        return found

    def close(self, state: str) -> None:
        """End one attempt without answering it: the person cancelled, or the callback failed and
        the attempt should not linger until the TTL."""
        if self._by_state.pop(state, None) is not None:
            self._order.remove(state)

    def _sweep(self) -> None:
        t = self._now()
        for state in [s for s, a in self._by_state.items() if a.expires_at <= t]:
            self._by_state.pop(state, None)
            self._order.remove(state)
