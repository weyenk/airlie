"""Wiring the OIDC sign-in together: one `Flow` per serve process.

Kept out of `oidc_flow.py` so that module stays free of `create_app`'s vocabulary, and out of `api.py` so the
routes stay about HTTP. The thing worth naming is `transport`: the same injectable seam `sources/github.py`
uses, so a functional test can run a whole sign-in — discovery, JWKS, token exchange — without a network, and
production passes None and gets httpx's real transport.

One IdP client and one attempt store per process, built here and handed to the router: the discovery document
and the JWKS are cached on the IdP, so sharing it is what stops a busy studio from putting a request on the
IdP per person."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from airlie.auth.oidc_flow import Attempts, Flow
from airlie.auth.oidc_http import ClientSecret, IdP
from airlie.auth.store import AuthStore
from airlie.config.schema import AirlieConfig

ATTEMPT_TTL_SECONDS = 300.0
MAX_ATTEMPTS = 1000
"""`/start` is public and free, so the store of attempts in flight is bounded — an unbounded dict there is a
way for a stranger to grow a serve process until it swaps."""


def build_oidc_flow(
    cfg: AirlieConfig,
    *,
    store: AuthStore,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], float],
) -> Flow | None:
    """The flow this install can run, or None when it has no IdP.

    None is the common case: `auth.oidc.enabled` is off by default, and every install that never mentions it
    keeps today's passkey and Tailscale sign-in unchanged. The routes answer 404 rather than explaining.

    `store` is the same `AuthStore` serve resolves every request against, and it satisfies `Bindings`
    directly: the table a sign-in reads is the table the admin routes write, with no copy of either to keep in
    step. It is required rather than optional because which rows decide a sign-in is not a detail a caller
    should be able to leave out.
    """
    spec = cfg.auth.oidc
    if not spec.enabled or spec.issuer is None:
        return None
    secret = ClientSecret(spec.client_secret_file) if spec.client_secret_file is not None else None
    return Flow(
        spec,
        IdP(spec.issuer, transport=transport, now=now),
        Attempts(now, ttl_seconds=ATTEMPT_TTL_SECONDS, max_pending=MAX_ATTEMPTS),
        now=now,
        secret=secret,
        bindings=_StoreBindings(store),
    )


class _StoreBindings:
    """`AuthStore.lookup_binding` behind the `Bindings` interface `Flow` holds.

    An adapter rather than `store` itself so the seam names the interface it satisfies — and, next to it, so
    an adapter that later needs to do more (an issuer allowlist, a cache) has somewhere to do it. The method
    it forwards is the whole of the callback's question: is this subject somebody airlie has?"""

    def __init__(self, store: AuthStore) -> None:
        self._store = store

    def lookup_binding(self, iss: str, sub: str) -> str | None:
        return self._store.lookup_binding(iss, sub)
