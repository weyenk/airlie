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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlencode

from airlie.auth.oidc import KeyRing, TokenRejected, verify_id_token
from airlie.auth.oidc_http import ClientSecret, IdP, IdPUnreachable, Provider, SecretRefused
from airlie.config.schema import OIDCSpec

#: How long a pending attempt lives. A person mid-redirect is typing nothing, so minutes are
#: enough, and it bounds how long a leaked state is worth replaying.
ATTEMPT_TTL_SECONDS = 300.0

#: Where a person who did not get a session goes: the studio's own sign-in screen, where the button that
#: started all this still is. A guarded route would only bounce them back here without a cookie.
_SIGNIN = "/signin"

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
    target: str = "/"


class Refused(Exception):
    """The attempt cannot become a session. `code` names the check that stopped it and is what reaches
    the log; `status` is the HTTP answer.

    No message carries IdP text, a token, a code or the client secret, so a route can log `code` and
    say a fixed sentence to the browser without deciding, per raise site, what is safe to repeat."""

    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class Unsigned(Exception):
    """The IdP did not answer: unreachable, an error reply, or a body airlie cannot read as a token
    reply. 502, because the person can retry and the thing that failed is not airlie."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def lookup_binding(iss: str, sub: str) -> str | None:
    """The user bound to this subject, or None when nobody is.

    Not the production path. serve builds a Flow over `auth/oidc_wiring.py`, which hands it the
    `oidc_bindings` table, so `Flow.callback` calls a method and never reaches this. It stays as the default
    for a Flow built without one — and the default must refuse, because an IdP assertion alone may never
    create or adopt a user (the spec's "no auto-link by email"). Keeping it here rather than making the
    parameter required is also what lets a test build a Flow over one dependency instead of two.
    """
    return None


class Bindings(Protocol):
    """What `Flow.callback` asks once a token verified: is this subject somebody airlie has?

    A `typing.Protocol` rather than a `(iss, sub) -> str | None` alias precisely because of `lookup_binding`
    above: a plain function object is not callable *through* an attribute, so `self._bindings(iss, sub)` would
    fail on one, while a method — `AuthStore.lookup_binding`, the real implementation — is. The protocol
    states the one thing the callback needs and nothing else, so the flow stays free of `auth.store`.
    """

    def lookup_binding(self, iss: str, sub: str) -> str | None: ...


class Outcome:
    """What a callback decided: `user_id` (sign this person in and take them to `target`), or `failure`
    naming the gate that stopped them with no user to sign in.

    An `Outcome` rather than an exception for a refusal because a refusal is a normal answer here — an
    unbound subject, a person who declined the consent screen — and the route owes those a fixed sentence,
    not a stack. `Unsigned` stays an exception because the IdP failing is a different event."""

    def __init__(
        self,
        *,
        user_id: str | None = None,
        failure: str | None = None,
        target: str = _SIGNIN,
        subject: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.failure = failure
        self.subject = subject
        # A refusal carries no destination of its own. The target arrived on a request that just demonstrated
        # it can be forged, and an anonymous browser does not belong on a guarded route, so this stays the
        # sign-in screen and the route answers a refusal with a JSON body rather than a redirect.
        self.target = target if user_id is not None else _SIGNIN


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


#: Where a sign-in begins and ends. Fixed strings, because the `redirect_uri` registered at the IdP is
#: `external_url` plus the callback path and must be byte-identical on every request.
CALLBACK_PATH = "/v1/auth/oidc/callback"


def _nothing_bound(iss: str, sub: str) -> str | None:
    """The answer for a Flow built without bindings: nobody. See `_NothingBound` for why the default is an
    object rather than this function, and `lookup_binding` for why refusing is the safe half of the pair."""
    return None


class _NothingBound:
    """The `Bindings` a Flow gets when built without one: nobody is ever bound.

    A class rather than a function so `self._bindings(iss, sub)` resolves the same way it does for the real
    store — and so "no bindings were wired" cannot read as a bug: it refuses, which is what an unbuilt link
    table should do.
    """

    def lookup_binding(self, iss: str, sub: str) -> str | None:
        return _nothing_bound(iss, sub)


class Flow:
    """The two halves of an authorization-code sign-in, over one `IdP`, one `Attempts` and one clock.

    The routes in `auth/api.py` stay thin — parse the query, call this, turn the result into a response —
    because every decision worth arguing about is here, where it can be tested without an HTTP layer: what
    goes to the IdP, which attempt a callback is allowed to answer, and what a verified subject may become.

    `idp` is injected rather than built here so serve shares one instance (and therefore one discovery and
    JWKS cache) across requests, and tests hand it a `MockTransport`.

    `bindings` is the link table (see `Bindings`). It has no production default: the value that a Flow built
    without one falls back to refuses every subject, which is the correct answer for a deployment whose
    bindings nobody has filled in, and the wrong one to reach for by accident.
    """

    def __init__(
        self,
        spec: OIDCSpec,
        idp: IdP,
        attempts: Attempts,
        *,
        now: Callable[[], float],
        secret: ClientSecret | None = None,
        bindings: Bindings | None = None,
    ) -> None:
        self._spec = spec
        self._idp = idp
        self._attempts = attempts
        self._now = now
        self._secret = secret
        self._bindings: Bindings = bindings if bindings is not None else _NothingBound()

    @property
    def enabled(self) -> bool:
        return self._spec.enabled

    @property
    def issuer(self) -> str:
        return self._idp.issuer

    def redirect_uri(self) -> str:
        """The `redirect_uri` for every request: the configured origin plus airlie's own path.

        Never derived from Host or Forwarded-Host — serve runs uvicorn with proxy_headers off, so those
        headers say what the caller typed. A redirect_uri an attacker chose is a code delivered to them, and
        the exchange that follows carries the client secret."""
        external = self._spec.external_url
        if not external:
            # config/schema.py requires it when enabled, so this is a programmatic config, not a typo
            raise Unsigned("external_url")
        return f"{external.rstrip('/')}{CALLBACK_PATH}"

    async def start(self, target: str | None) -> str:
        """The URL to send the browser to, having saved what its callback will have to answer.

        Raises `Unsigned` when the IdP cannot be reached for discovery: a redirect to a page that will fail
        at the callback is worse than a 502 now, and the person retrying is what clears it.

        The default target is the studio root, not the sign-in screen. `safe_target` also accepts `/signin`,
        and the callback sends a *failure* there, so one default would either strand someone who signed in
        successfully on the very screen they just left, or bounce someone who failed onto a guarded route."""
        provider = await self._provider()
        attempt = new_attempt(redirect_uri=self.redirect_uri(), now=self._now())
        saved = replace(attempt, target=safe_target(target, default="/"))
        self._attempts.open(saved)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._spec.client_id or "",
                "redirect_uri": saved.redirect_uri,
                "scope": " ".join(self._spec.scopes),
                "state": saved.state,
                "nonce": saved.nonce,
                "code_challenge": code_challenge(saved.verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{provider.authorize_endpoint}?{query}"

    async def callback(self, query: Mapping[str, str]) -> Outcome:
        """Close an attempt and decide what it means. Returns an Outcome rather than raising for a refusal
        the person caused, so the route logs one line and answers a fixed sentence.

        Order is the order that costs an attacker the most and airlie the least: match and consume the state
        (so nothing is sent to the IdP for a callback that answers no request of ours), then exchange, then
        verify the token against the saved nonce, then apply airlie's own gates, then look for a binding."""
        state = query.get("state") or ""
        attempt = self._attempts.take(state)
        if attempt is None:
            raise Refused("no_attempt")  # 400: forged, expired, or already answered
        if query.get("error"):
            # The IdP says the person declined or the request was bad. Their attempt is already spent.
            return Outcome(failure="declined")
        code = query.get("code") or ""
        if not code:
            return Outcome(failure="no_code")

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": attempt.redirect_uri,
            "client_id": self._spec.client_id or "",
            "code_verifier": attempt.verifier,
        }
        secret = self._client_secret()
        if secret:
            form["client_secret"] = secret
        reply = await self._exchange(form)
        token = reply.get("id_token")
        if not isinstance(token, str) or not token:
            raise Unsigned("no_id_token")

        keys = await self._keys()
        try:
            claims = verify_id_token(
                token,
                keys,
                issuer=self._idp.issuer,
                audience=self._spec.client_id or "",
                nonce=attempt.nonce,
                now=self._now(),
                allowed_domains=tuple(self._spec.allowed_domains),
                groups_claim=self._spec.groups_claim,
                allowed_groups=tuple(self._spec.allowed_groups),
                admin_role_claim=self._spec.admin_role_claim,
            )
        except TokenRejected as e:
            raise Refused(e.code, 401) from e
        except (TypeError, ValueError) as e:
            # a key the JWT library cannot use is the IdP's problem, not the person's
            raise Unsigned("key") from e

        user_id = self._bindings.lookup_binding(claims.iss, claims.sub)
        if user_id is None:
            # The verified subject is what an admin binds, so it is carried out for the log. The spec refuses
            # to guess: no user is created, and an email is never the key ("no auto-link by email").
            return Outcome(failure="unbound", subject=claims.sub)
        return Outcome(user_id=user_id, target=attempt.target)

    async def _provider(self) -> Provider:
        """Discovery, with the IdP's own failure translated into airlie's.

        `IdP` raises `IdPUnreachable`, which quotes a status or an exception type; `Unsigned` is the type the
        routes answer 502 for and that carries nothing worth leaking. Translating here rather than letting the
        route import both types keeps every IdP-facing failure answering the same way."""
        try:
            return await self._idp.provider()
        except IdPUnreachable as e:
            raise Unsigned(e.code) from e

    async def _keys(self) -> KeyRing:
        """The IdP's keys, translated the same way discovery is.

        A failure to read them is 502 rather than 401 deliberately: an unknown `kid` is already resolved by
        `IdP.key_ring(force=...)`'s one refetch inside `verify`, so anything still failing here is the IdP
        being unreadable, and telling the person "your IdP said no" would send them to the wrong door."""
        try:
            return await self._idp.key_ring()
        except IdPUnreachable as e:
            raise Unsigned(e.code) from e

    async def _exchange(self, form: dict[str, str]) -> Mapping[str, Any]:
        try:
            reply = await self._idp.exchange(form)
        except IdPUnreachable as e:
            raise Unsigned(e.code) from e
        if reply.get("error"):
            # The IdP's `error_description` often quotes our own request, secret included: only the code
            # is ours to keep, and `IdP.exchange` masks the values before they get this far.
            raise Unsigned("token_refused")
        return reply

    def _client_secret(self) -> str | None:
        """The configured secret, or None for a public client. A file that cannot be trusted is a refusal
        naming the setting, not a secret worth trying without."""
        if self._secret is None:
            return None
        try:
            return self._secret.read()
        except SecretRefused:
            raise Unsigned("client_secret") from None
