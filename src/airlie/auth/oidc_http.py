"""What the IdP publishes: its endpoints (discovery) and its public keys (JWKS).

Fetches live here, not in `auth/oidc.py`, because that module must stay pure — see its docstring. Discovery
is cached because a sign-in reads it twice (once to build the authorize URL, once to exchange the code) and
the answer does not change during either. JWKS is keyed by `kid` and refreshed on an unknown `kid` at most
once per refresh interval, so a key rotation does not break sign-ins and a stranger cannot make airlie fetch
on every request by naming a key that is not there (spec "JWKS cache keyed by kid").

The endpoints are validated against the configured issuer before anything is sent to them. A discovery
document is data the IdP controls; the issuer is the value an operator chose, and the one that verification
requires an ID token to carry. If they disagree, the pair is unusable as a pair: accepting the document's
endpoints would send the authorization response, the PKCE verifier and the client secret to a host that is
not the one whose tokens we later insist on verifying.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from .oidc import KeyRing

TIMEOUT = httpx.Timeout(15.0, connect=5.0)

#: How often JWKS may be re-fetched, and how long discovery may be trusted. Rotations are planned and
#: tokens are short-lived, so both are generous; the interval exists to bound the harm of an unknown kid.
JWKS_REFRESH_SECONDS = 300.0
DISCOVERY_TTL_SECONDS = 3600.0


class IdPUnreachable(Exception):
    """The IdP did not answer usefully. `code` is for logs; the message quotes no IdP body, no key and no
    token, following `sources/github.py`, which masks what it sent before it says anything."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def _origin(url: str) -> str:
    """The scheme://host[:port] of a URL, lower-cased on the scheme and host the way RFC 3986 says they are
    case-insensitive.

    A prefix test would pass for `https://id.acme.com.evil.net`, and a comparison that ignored the port would
    pass for a host we were never issued certificates for. httpx normalises an explicit `:443` away, so
    `https://id.acme.com:443/x` and `https://id.acme.com/x` share an origin here rather than being two."""
    parts = httpx.URL(url)
    host = (parts.host or "").lower()
    if not host:
        return ""
    scheme = parts.scheme.lower()
    return f"{scheme}://{host}" if parts.port is None else f"{scheme}://{host}:{parts.port}"


def _same_origin(found: str, issuer: str) -> bool:
    """Whether `found` shares the issuer's origin — same scheme, host and port, any path.

    Paths are allowed (`https://id.acme.com/tenant/default` is a normal issuer). The path is also what lets an
    issuer cover several tenants, so requiring it to match would break real configurations without adding
    anything: the authority is the part that decides who signed the token."""
    f, i = _origin(found), _origin(issuer)
    return bool(f) and f == i


def _field(document: Any, name: str) -> str:
    value = document.get(name) if isinstance(document, dict) else None
    if not isinstance(value, str) or not value:
        raise IdPUnreachable("discovery", f"{name} is missing")
    return value


@dataclass(frozen=True)
class Provider:
    """The endpoints a sign-in needs, already checked against the configured issuer."""

    authorize_endpoint: str
    token_endpoint: str
    jwks_uri: str

    @staticmethod
    def from_document(document: Any, *, issuer: str) -> Provider:
        """The three endpoints airlie uses, each required to live on the configured issuer's origin.

        A document may carry endpoints for features we do not use (`userinfo_endpoint`,
        `end_session_endpoint`)
        and they are not read; Slice D may need the logout one, and until then fetching nothing is the safe
        default."""
        if not isinstance(document, dict):
            raise IdPUnreachable("discovery", "the reply is not a JSON object")
        found = {
            "authorize_endpoint": ("authorization_endpoint", _field(document, "authorization_endpoint")),
            "token_endpoint": ("token_endpoint", _field(document, "token_endpoint")),
            "jwks_uri": ("jwks_uri", _field(document, "jwks_uri")),
        }
        endpoints = {}
        for field_name, (document_name, url) in found.items():
            if not _same_origin(url, issuer):
                raise IdPUnreachable("discovery", f"{document_name} is not on the configured issuer's origin")
            endpoints[field_name] = url
        return Provider(**endpoints)


class ProviderDocs:
    """Discovery documents by issuer, in memory, with a TTL."""

    def __init__(self, *, ttl_seconds: float = DISCOVERY_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._by_issuer: dict[str, tuple[float, Provider]] = {}

    def cached(self, issuer: str, now: float) -> Provider | None:
        found = self._by_issuer.get(issuer)
        if found is None:
            return None
        expires_at, provider = found
        if expires_at <= now:
            del self._by_issuer[issuer]
            return None
        return provider

    def remember(self, issuer: str, provider: Provider, now: float) -> None:
        self._by_issuer[issuer] = (now + self._ttl, provider)

    def forget(self, issuer: str) -> None:
        self._by_issuer.pop(issuer, None)


class KeySets:
    """JWKS by issuer, kept as the parsed ring plus the moment it was fetched.

    Entries are keyed by the *pair* (issuer, kid), so one issuer cannot overwrite another's key that happens
    to carry the same kid — realistic with a shared `kid` scheme like `v1` across two tenants. An entry is
    retired after `max_keys`, which lets the newest generation in and keeps the set bounded: an IdP publishing
    two keys a day would otherwise grow the ring forever. A token signed by a key we have already retired
    forces a refetch rather than verifying against the wrong material.

    A forced refresh serves the previous key set while the interval holds: a token naming a kid we have not
    seen is refused on the spot, and the one retry that re-fetches may use what it already has, since
    otherwise
    a first sign-in after a rotation fails twice."""

    def __init__(self, *, refresh_seconds: float = JWKS_REFRESH_SECONDS, max_keys: int = 64) -> None:
        self._refresh = refresh_seconds
        self._max = max_keys
        self._rings: OrderedDict[tuple[str, str], tuple[float, KeyRing]] = OrderedDict()
        self._last_fetch: dict[str, float] = {}

    def fresh(self, issuer: str, now: float) -> KeyRing | None:
        """The key set covering every kid we hold for this issuer, if the newest of them is inside the refresh
        window; None when a fetch is due."""
        held = self._merged(issuer)
        if held is None:
            return None
        newest, ring = held
        return ring if now - newest < self._refresh else None

    def stale(self, issuer: str) -> KeyRing | None:
        """Whatever is held for this issuer, however old — the fallback while a refetch is not yet due."""
        held = self._merged(issuer)
        return None if held is None else held[1]

    def may_fetch(self, issuer: str, now: float) -> bool:
        """Whether a fetch is allowed now. The interval is per issuer, so one busy tenant cannot starve
        another of its rotation."""
        last = self._last_fetch.get(issuer)
        return last is None or now - last >= self._refresh

    def remember(self, issuer: str, ring: KeyRing, now: float) -> None:
        for kid, key in ring.by_kid.items():
            self._rings[(issuer, kid)] = (now, KeyRing(by_kid={kid: key}))
            self._rings.move_to_end((issuer, kid))
        while len(self._rings) > self._max:
            self._rings.popitem(last=False)
        self._last_fetch[issuer] = now

    def mark_fetch(self, issuer: str, now: float) -> None:
        """Record an attempt that did not produce a key set, so a broken IdP is not hammered."""
        self._last_fetch[issuer] = now

    def forget(self, issuer: str) -> None:
        for pair in [p for p in self._rings if p[0] == issuer]:
            del self._rings[pair]
        self._last_fetch.pop(issuer, None)

    def _merged(self, issuer: str) -> tuple[float, KeyRing] | None:
        """The held keys for one issuer and the moment the newest was fetched."""
        by_kid: dict[str, Any] = {}
        newest = 0.0
        for (ring_issuer, kid), (fetched_at, ring) in self._rings.items():
            if ring_issuer != issuer:
                continue
            by_kid[kid] = ring.by_kid[kid]
            newest = max(newest, fetched_at)
        if not by_kid:
            return None
        return newest, KeyRing(by_kid=by_kid)


def key_ring_from_jwks(document: Any) -> KeyRing:
    """The `kid` -> verification key map from a JWKS document.

    Only ktypes we will actually verify are kept, and a key without a `kid` is dropped: `KeyRing.key_for` is
    keyed by the header's `kid`, so a key nothing can name is dead weight. A symmetric (`kty: oct`) entry is
    dropped with the rest — it is not in the allowlist so `verify_id_token` would never select it, and keeping
    it here only risks a future caller treating the ring as "keys the IdP offered".

    Malformed entries are skipped rather than fatal: a document mixing a good key with a corrupt one should
    still let the good key sign someone in."""
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list):
        raise IdPUnreachable("jwks", "the reply carries no key list")
    by_kid: dict[str, Any] = {}
    for entry in keys:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if entry.get("kty") not in ("RSA", "EC"):
            continue
        try:
            by_kid[kid] = jwt.PyJWK.from_dict(dict(entry)).key
        except (jwt.InvalidKeyError, KeyError, ValueError, TypeError):
            continue
    if not by_kid:
        raise IdPUnreachable("jwks", "no usable key")
    return KeyRing(by_kid=by_kid)


class IdP:
    """One issuer's discovery document and key set, fetched over an injectable transport.

    `transport` is `None` for a real sign-in and a `MockTransport` in tests, the same seam
    `sources/github.py` uses; there is no network in the unit suite.
    """

    def __init__(
        self,
        issuer: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] = time.time,
        docs: ProviderDocs | None = None,
        key_sets: KeySets | None = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._transport = transport
        self._now = now
        # A fresh KeySets per instance unless the caller shares one. The default must not be a class
        # attribute: several IdP objects built in one process (two tenants, or two tests) would otherwise
        # silently share a fetch budget, and the failure would look like a cache that never expires.
        self._docs = docs or ProviderDocs()
        self._keys = key_sets or KeySets()

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def discovery_url(self) -> str:
        """`/.well-known/openid-configuration` under the issuer's path, per OIDC.

        The issuer's own path comes first: `https://id.acme.com/tenant/default` publishes at
        `/tenant/default/.well-known/openid-configuration`, and appending to the origin would fetch the wrong
        tenant's document and then fail the origin check in a confusing way."""
        parts = httpx.URL(self._issuer)
        path = parts.path.rstrip("/")
        return f"{_origin(self._issuer)}{path}/.well-known/openid-configuration"

    async def _get_json(self, url: str) -> Any:
        try:
            async with httpx.AsyncClient(
                headers={"Accept": "application/json"},
                transport=self._transport,
                timeout=TIMEOUT,
                follow_redirects=False,
            ) as client:
                r = await client.get(url)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise IdPUnreachable("unreachable", f"{type(e).__name__}, no reply") from e
        if r.status_code != 200:
            raise IdPUnreachable("unreachable", f"{r.status_code}")
        try:
            return r.json()
        except (ValueError, RecursionError) as e:
            raise IdPUnreachable("unreachable", "the reply is not JSON") from e

    async def provider(self) -> Provider:
        cached = self._docs.cached(self._issuer, self._now())
        if cached is not None:
            return cached
        document = await self._get_json(self.discovery_url)
        provider = Provider.from_document(document, issuer=self._issuer)
        self._docs.remember(self._issuer, provider, self._now())
        return provider

    async def key_ring(self, *, force: bool = False) -> KeyRing:
        """The IdP's current public keys, fetched at most once per refresh interval unless `force`."""
        now = self._now()
        if not force:
            fresh = self._keys.fresh(self._issuer, now)
            if fresh is not None:
                return fresh
        if not self._keys.may_fetch(self._issuer, now):
            stale = self._keys.stale(self._issuer)
            if stale is not None:
                return stale
            raise IdPUnreachable("refresh", "keys are not due for another fetch")
        # mark_fetch before the fetch, not after: the attempt itself is what must be throttled, so an issuer
        # whose discovery document is failing cannot be re-read once per sign-in. A successful document does
        # not need that protection — ProviderDocs already caches it — so remembering the attempt here costs
        # nothing on the healthy path.
        self._keys.mark_fetch(self._issuer, now)
        provider = await self.provider()
        try:
            document = await self._get_json(provider.jwks_uri)
        except IdPUnreachable:
            # A rotation that lands while the JWKS host is briefly down should not lock the tenant out
            # for the whole interval; the previous keys are still the best guess for a just-rotated kid.
            stale = self._keys.stale(self._issuer)
            if stale is None:
                raise
            return stale
        try:
            ring = key_ring_from_jwks(document)
        except IdPUnreachable:
            stale = self._keys.stale(self._issuer)
            if stale is None:
                raise
            return stale
        self._keys.remember(self._issuer, ring, self._now())
        return ring

    async def exchange(self, form: dict[str, str]) -> dict[str, Any]:
        """POST the authorization-code exchange and return the JSON body.

        Nothing here verifies the ID token — that is the callback's job with the saved nonce. This only moves
        bytes, and refuses to describe them: `id_token` and `access_token` never appear in an error, and the
        client secret is masked from anything the IdP echoes back about what we sent."""
        provider = await self.provider()
        failure = ""
        body: Any = None
        try:
            async with httpx.AsyncClient(
                headers={"Accept": "application/json"},
                transport=self._transport,
                timeout=TIMEOUT,
                follow_redirects=False,
            ) as client:
                r = await client.post(provider.token_endpoint, data=form)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            failure = f"{type(e).__name__}, no reply"
        else:
            try:
                body = r.json()
            except (ValueError, RecursionError):
                body = None
            if not isinstance(body, dict):
                failure = f"{r.status_code}, but the reply is not a JSON object"
        if failure:
            raise IdPUnreachable("token", failure)
        assert isinstance(body, dict)
        said = body.get("error_description")
        if isinstance(said, str):
            # The error code itself is ours to show; the description is the IdP quoting what we sent back at
            # us, so it is kept only after every value we posted in it has been masked.
            for value in filter(None, form.values()):
                said = said.replace(value, "***")
            body["error_description"] = said
        return body
