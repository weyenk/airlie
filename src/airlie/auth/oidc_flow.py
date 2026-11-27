"""The OIDC authorization-code flow: an attempt in flight, and the routes that open and close one.

Split from `auth/oidc.py` on purpose. That module is pure verification — token and key ring in, claims or a
refusal out — and it reads no clock and fetches nothing, which is what makes every rejection testable. This
module holds the stateful half: a store with a clock, and HTTP. Keeping them apart is what lets the
verification stay honest.

The flow (docs/superpowers/specs/2026-11-26-oidc-sign-in-design.md):

    /start   makes state, nonce and a PKCE verifier, saves them, and sends the browser to the IdP
    /callback matches and consumes the state, exchanges the code with the verifier, verifies the ID token
             against the saved nonce, applies airlie's gates, and mints the session a passkey login would get

A callback that ends in a session is indistinguishable from a passkey login to everything downstream:
`Resolver.resolve()` never learns which sign-in produced the cookie."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Pending:
    """One browser interaction that asked to sign in, and the three secrets that make its callback answerable
    only by that browser.

    There is deliberately no user here: airlie does not know who is signing in until an ID token has been
    verified, and an attempt that could pre-claim a person would let a callback complete as someone the
    browser never proved."""

    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    expires_at: float


class Attempts:
    """Authorization attempts in flight, in memory, keyed by `state`. One per serve process, like the WebAuthn
    challenges: a restart ends them and a person mid-redirect starts again.

    /start is a public route, so the store is bounded — an unbounded dict there is a way for a stranger to
    grow a serve process until it swaps."""

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
        """Save an attempt, evicting what has already expired and then the oldest if still at the cap."""
        self._sweep()
        self._by_state.pop(attempt.state, None)
        while len(self._by_state) >= self._max and self._order:
            self._by_state.pop(self._order.popleft(), None)
        self._by_state[attempt.state] = attempt
        self._order.append(attempt.state)

    def take(self, state: str) -> Pending | None:
        """The attempt this `state` answers, if it is still live — and it is gone either way.

        A code is single-use, so a second callback carrying the same state is a replay rather than a refresh,
        and handing the same verifier to the IdP twice would fail with a more confusing error than
        refusing."""
        found = self._by_state.pop(state, None)
        if found is not None:
            self._order.remove(state)
        if found is None or found.expires_at <= self._now():
            return None
        return found

    def close(self, state: str) -> None:
        """End one attempt without answering it: the person cancelled, or the callback failed and the attempt
        should not linger until the TTL."""
        if self._by_state.pop(state, None) is not None:
            self._order.remove(state)

    def _sweep(self) -> None:
        t = self._now()
        for state in [s for s, a in self._by_state.items() if a.expires_at <= t]:
            self._by_state.pop(state, None)
            self._order.remove(state)
