"""Discovery and JWKS fetching for the OIDC flow: what we accept from an IdP's published documents, and what
we refuse.

Three things are worth the test count.

First, the endpoints in a discovery document are the IdP's choice, so they are checked against the configured
*issuer* before anything is sent to them — a document naming a foreign token endpoint would take the
authorization code, the PKCE verifier and the client secret with it.

Second, a key set is never fetched per request. An unknown `kid` is trivial for anyone to name in a forged
header, so if each one triggered an unthrottled fetch, a stranger holding only the callback URL would own the
IdP's request budget.

Third, keys are cached per (issuer, kid), not per issuer: two tenants whose IdPs both label a key `v1` must
not
verify each other's tokens.

Keys are generated here and a MockTransport answers, so no test touches the network and every rejection can be
made on purpose. Mirrors the transport seam of tests/unit/test_github_source.py."""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx
import jwt
import jwt.algorithms as algorithms
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.api_jws import PyJWS

from airlie.auth.oidc import KeyRing, TokenRejected
from airlie.auth.oidc_http import (
    DISCOVERY_TTL_SECONDS,
    JWKS_REFRESH_SECONDS,
    IdP,
    IdPUnreachable,
    KeySets,
    Provider,
    ProviderDocs,
    key_ring_from_jwks,
)

ISSUER = "https://id.acme.com"
OTHER = "https://other.example"
JWKS_URI = f"{ISSUER}/keys"

RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC = ec.generate_private_key(ec.SECP256R1())


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def rsa_jwk(kid: str) -> dict[str, Any]:
    entry: dict[str, Any] = algorithms.RSAAlgorithm.to_jwk(RSA.public_key(), as_dict=True)
    entry["kid"] = kid
    entry["use"] = "sig"
    return entry


def ec_jwk(kid: str) -> dict[str, Any]:
    entry: dict[str, Any] = algorithms.ECAlgorithm.to_jwk(EC.public_key(), as_dict=True)
    entry["kid"] = kid
    return entry


def parse_form(request: httpx.Request) -> dict[str, str]:
    """The form the IdP actually received, decoded rather than split by hand — which matters because a secret
    containing `=` or `&` must still read back correctly."""
    return dict(urllib.parse.parse_qsl(request.content.decode(), keep_blank_values=True))


class FakeIdP:
    """A stand-in IdP answering its own discovery, JWKS and token endpoints, recording every request."""

    def __init__(
        self,
        issuer: str = ISSUER,
        *,
        document: dict[str, Any] | None = None,
        jwks: dict[str, Any] | None = None,
    ) -> None:
        self.issuer = issuer
        self.jwks_uri = f"{issuer}/keys"
        self.authorize_url = f"{issuer}/authorize"
        self.token_url = f"{issuer}/token"
        self.document = document if document is not None else self.document_for()
        self.jwks = jwks if jwks is not None else {"keys": [rsa_jwk("k1")]}
        self.requests: list[str] = []
        self.token_bodies: list[dict[str, str]] = []
        self.token_reply: Any = {"access_token": "a-token", "id_token": "a.jwt"}
        self.jwks_status = 200
        self.document_status = 200

    def document_for(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "authorization_endpoint": self.authorize_url,
            "token_endpoint": self.token_url,
            "jwks_uri": self.jwks_uri,
        }
        base.update(overrides)
        return base

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            if self.document_status != 200:
                return httpx.Response(self.document_status, text="nope")
            return httpx.Response(200, json=self.document)
        if url == self.jwks_uri:
            if self.jwks_status != 200:
                return httpx.Response(self.jwks_status, text="nope")
            return httpx.Response(200, json=self.jwks)
        if url == self.token_url:
            self.token_bodies.append(parse_form(request))
            if isinstance(self.token_reply, dict):
                return httpx.Response(200, json=self.token_reply)
            return httpx.Response(400, text=str(self.token_reply))
        return httpx.Response(404, text=f"no route {url}")

    @property
    def discovery_calls(self) -> int:
        return sum(1 for u in self.requests if ".well-known" in u)

    @property
    def jwks_calls(self) -> int:
        return sum(1 for u in self.requests if u == self.jwks_uri)


def multi(*fakes: FakeIdP) -> httpx.MockTransport:
    """One transport for several issuers, routed on the issuer in the URL — so a test about two tenants never
    has to hand-write a router that forgets an endpoint."""

    def route(request: httpx.Request) -> httpx.Response:
        for fake in fakes:
            if str(request.url).startswith(fake.issuer):
                return fake.handle(request)
        raise AssertionError(f"unrouted request {request.url}")

    return httpx.MockTransport(route)


def idp(fake: FakeIdP, now: Clock | None = None, **kwargs: Any) -> IdP:
    return IdP(fake.issuer, transport=fake.transport(), now=now or Clock(), **kwargs)


# --- discovery -----------------------------------------------------------------------------------------


async def test_discovery_is_fetched_from_the_well_known_url_under_the_issuer() -> None:
    fake = FakeIdP()
    provider = await idp(fake).provider()
    assert fake.discovery_calls == 1
    assert provider.authorize_endpoint == fake.authorize_url
    assert provider.token_endpoint == fake.token_url
    assert provider.jwks_uri == JWKS_URI


async def test_a_tenant_issuer_publishes_under_its_own_path() -> None:
    """The common multi-tenant shape. Appending to the origin instead would read another tenant's
    endpoints."""
    fake = FakeIdP(issuer="https://id.acme.com/t/7")
    await idp(fake).provider()
    assert fake.requests == ["https://id.acme.com/t/7/.well-known/openid-configuration"]


async def test_a_trailing_slash_on_the_issuer_does_not_double_the_path() -> None:
    fake = FakeIdP()
    subject = IdP(ISSUER + "/", transport=fake.transport(), now=Clock())
    assert subject.discovery_url == f"{ISSUER}/.well-known/openid-configuration"
    await subject.provider()
    assert fake.discovery_calls == 1


async def test_discovery_is_cached_and_reread_when_it_expires() -> None:
    fake = FakeIdP()
    clock = Clock()
    subject = idp(fake, clock)
    await subject.provider()
    await subject.provider()
    assert fake.discovery_calls == 1
    clock.t += DISCOVERY_TTL_SECONDS + 1
    await subject.provider()
    assert fake.discovery_calls == 2


async def test_a_document_that_is_not_an_object_is_refused() -> None:
    for bad in ([], "text", None, 7):
        fake = FakeIdP()
        fake.document = bad  # type: ignore[assignment]
        with pytest.raises(IdPUnreachable):
            await idp(fake).provider()


async def test_a_missing_endpoint_is_refused_rather_than_defaulted() -> None:
    """Guessing an endpoint would send a code somewhere nobody checked."""
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        document = FakeIdP().document_for()
        del document[name]
        fake = FakeIdP(document=document)
        with pytest.raises(IdPUnreachable):
            await idp(fake).provider()


async def test_an_endpoint_on_another_host_is_refused() -> None:
    """The whole point of checking: a document naming a foreign token endpoint would take the client secret
    there."""
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        fake = FakeIdP(document=FakeIdP().document_for(**{name: f"https://id.acme.com.evil.net/{name}"}))
        with pytest.raises(IdPUnreachable):
            await idp(fake).provider()


async def test_a_prefix_lookalike_host_is_refused() -> None:
    """A plain startswith check on the issuer would pass for this."""
    fake = FakeIdP(document=FakeIdP().document_for(token_endpoint="https://id.acme.com.evil.net/token"))
    with pytest.raises(IdPUnreachable):
        await idp(fake).provider()


async def test_a_different_port_is_a_different_origin() -> None:
    fake = FakeIdP(document=FakeIdP().document_for(token_endpoint="https://id.acme.com:8443/token"))
    with pytest.raises(IdPUnreachable):
        await idp(fake).provider()


async def test_an_explicit_default_port_is_the_same_origin() -> None:
    """httpx normalises `:443` away; refusing it would break an IdP that spells its endpoints out fully."""
    fake = FakeIdP(document=FakeIdP().document_for(token_endpoint="https://id.acme.com:443/token"))
    provider = await idp(fake).provider()
    assert provider.token_endpoint == "https://id.acme.com:443/token"


async def test_a_different_scheme_is_refused() -> None:
    fake = FakeIdP(document=FakeIdP().document_for(token_endpoint="http://id.acme.com/token"))
    with pytest.raises(IdPUnreachable):
        await idp(fake).provider()


async def test_a_different_path_on_the_same_origin_is_accepted() -> None:
    """Real IdPs publish under a path; refusing it would break working configurations."""
    fake = FakeIdP(document=FakeIdP().document_for(token_endpoint="https://id.acme.com/oauth2/v1/token"))
    provider = await idp(fake).provider()
    assert provider.token_endpoint.endswith("/oauth2/v1/token")


async def test_the_issuer_host_is_matched_case_insensitively() -> None:
    fake = FakeIdP(document=FakeIdP().document_for(token_endpoint="https://ID.ACME.COM/token"))
    provider = await idp(fake).provider()
    assert provider.token_endpoint == "https://ID.ACME.COM/token"


async def test_an_endpoint_that_is_not_a_string_is_refused() -> None:
    fake = FakeIdP(document=FakeIdP().document_for(jwks_uri=["https://id.acme.com/keys"]))
    with pytest.raises(IdPUnreachable):
        await idp(fake).provider()


async def test_a_non_200_is_unreachable_and_quotes_no_body() -> None:
    for status in (401, 404, 500):

        def answer(request: httpx.Request, status: int = status) -> httpx.Response:
            return httpx.Response(status, text="SECRET UPSTREAM TEXT")

        subject = IdP(ISSUER, transport=httpx.MockTransport(answer), now=Clock())
        with pytest.raises(IdPUnreachable) as exc:
            await subject.provider()
        assert "SECRET" not in str(exc.value)


async def test_a_reply_that_is_not_json_is_unreachable() -> None:
    subject = IdP(
        ISSUER, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")), now=Clock()
    )
    with pytest.raises(IdPUnreachable):
        await subject.provider()


async def test_a_transport_failure_is_unreachable_not_a_crash() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    subject = IdP(ISSUER, transport=httpx.MockTransport(refuse), now=Clock())
    with pytest.raises(IdPUnreachable):
        await subject.provider()


async def test_redirects_are_not_followed() -> None:
    """Following them would let a document we refused become a request we made."""
    seen: list[str] = []

    def hop(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": f"{ISSUER}/token"})

    subject = IdP(ISSUER, transport=httpx.MockTransport(hop), now=Clock())
    with pytest.raises(IdPUnreachable):
        await subject.provider()
    assert len(seen) == 1


# --- the provider document and its cache ---------------------------------------------------------------


def test_a_provider_can_be_built_from_a_document_directly() -> None:
    provider = Provider.from_document(FakeIdP().document_for(), issuer=ISSUER)
    assert provider.jwks_uri == JWKS_URI


def test_an_empty_string_endpoint_is_refused_not_treated_as_absent() -> None:
    with pytest.raises(IdPUnreachable):
        Provider.from_document(FakeIdP().document_for(token_endpoint=""), issuer=ISSUER)


def test_a_provider_from_an_issuerless_document_is_refused() -> None:
    """An empty configured issuer must not match everything."""
    with pytest.raises(IdPUnreachable):
        Provider.from_document(FakeIdP().document_for(), issuer="")


def test_discovery_cache_forget_ends_trust_in_a_document() -> None:
    """A config reload must not keep using endpoints read from an issuer that has since changed."""
    docs = ProviderDocs()
    clock = Clock()
    provider = Provider.from_document(FakeIdP().document_for(), issuer=ISSUER)
    docs.remember(ISSUER, provider, clock())
    assert docs.cached(ISSUER, clock()) == provider
    docs.forget(ISSUER)
    assert docs.cached(ISSUER, clock()) is None


def test_discovery_cache_is_per_issuer() -> None:
    docs = ProviderDocs()
    clock = Clock()
    other = Provider.from_document(FakeIdP(issuer=OTHER).document_for(), issuer=OTHER)
    docs.remember(OTHER, other, clock())
    assert docs.cached(ISSUER, clock()) is None
    assert docs.cached(OTHER, clock()) == other


# --- JWKS parsing -------------------------------------------------------------------------------------


def test_a_jwks_document_becomes_a_key_ring_keyed_by_kid() -> None:
    ring = key_ring_from_jwks({"keys": [rsa_jwk("k1"), ec_jwk("k2")]})
    assert set(ring.by_kid) == {"k1", "k2"}


def test_an_rsa_key_verifies_an_rs256_token() -> None:
    """Not just that parsing ran: the key it produced must accept a real signature, which is what the
    callback's verification depends on."""
    ring = key_ring_from_jwks({"keys": [rsa_jwk("k1")]})
    token = jwt.encode({"sub": "u1"}, RSA, algorithm="RS256", headers={"kid": "k1"})
    PyJWS().decode_complete(token, ring.key_for("k1"), algorithms=("RS256", "ES256"))


def test_an_ec_key_verifies_an_es256_token() -> None:
    """The other half: the EC path is usable, not merely parsed, which is what lets an IdP ship ES256."""
    ring = key_ring_from_jwks({"keys": [ec_jwk("e1")]})
    token = jwt.encode({"sub": "u1"}, EC, algorithm="ES256", headers={"kid": "e1"})
    PyJWS().decode_complete(token, ring.key_for("e1"), algorithms=("RS256", "ES256"))


def test_the_key_ring_refuses_a_token_signed_by_another_key() -> None:
    """Proves the ring holds the *right* key: an ES256 token must not verify against the RSA key that happens
    to carry the same kid."""
    ring = key_ring_from_jwks({"keys": [rsa_jwk("k1")]})
    token = jwt.encode({"sub": "u1"}, EC, algorithm="ES256", headers={"kid": "k1"})
    with pytest.raises((jwt.PyJWTError, TypeError)):
        PyJWS().decode_complete(token, ring.key_for("k1"), algorithms=("RS256", "ES256"))


def test_a_key_the_ring_does_not_name_is_refused_by_kid() -> None:
    ring = key_ring_from_jwks({"keys": [rsa_jwk("k1")]})
    with pytest.raises(TokenRejected) as exc:
        ring.key_for("nope")
    assert exc.value.code == "unknown_kid"


def test_a_key_without_a_kid_is_dropped() -> None:
    """Nothing can name it, so it can only ever be dead weight in the ring."""
    orphan: dict[str, Any] = algorithms.RSAAlgorithm.to_jwk(RSA.public_key(), as_dict=True)
    ring = key_ring_from_jwks({"keys": [orphan, rsa_jwk("k1")]})
    assert set(ring.by_kid) == {"k1"}


def test_a_symmetric_key_is_dropped() -> None:
    """An IdP has no business publishing an HMAC key. `verify_id_token` would never pick it — the allowlist is
    RS256/ES256 — but the ring should not hold one either."""
    ring = key_ring_from_jwks({"keys": [{"kty": "oct", "k": "a" * 32, "kid": "o1"}, rsa_jwk("k1")]})
    assert set(ring.by_kid) == {"k1"}


def test_a_malformed_entry_does_not_sink_the_good_keys() -> None:
    ring = key_ring_from_jwks({"keys": [{"kty": "bogus", "kid": "b"}, "not even an object", rsa_jwk("k1")]})
    assert set(ring.by_kid) == {"k1"}


def test_a_document_with_no_usable_key_is_refused() -> None:
    for document in ({"keys": []}, {}, {"keys": "nope"}, {"keys": [{"kty": "oct", "k": "x" * 32}]}):
        with pytest.raises(IdPUnreachable):
            key_ring_from_jwks(document)


# --- key-set cache ------------------------------------------------------------------------------------


async def test_jwks_is_fetched_through_the_documented_uri() -> None:
    fake = FakeIdP()
    ring = await idp(fake).key_ring()
    assert fake.jwks_calls == 1
    assert "k1" in ring.by_kid
    assert fake.requests == [f"{ISSUER}/.well-known/openid-configuration", JWKS_URI]


async def test_a_known_kid_never_refetches() -> None:
    fake = FakeIdP()
    subject = idp(fake)
    await subject.key_ring()
    await subject.key_ring()
    await subject.key_ring()
    assert fake.jwks_calls == 1


async def test_keys_are_refetched_only_once_the_refresh_interval_has_passed() -> None:
    fake = FakeIdP()
    clock = Clock()
    subject = idp(fake, clock)
    await subject.key_ring()
    for _ in range(20):
        await subject.key_ring(force=True)
    assert fake.jwks_calls == 1
    clock.t += JWKS_REFRESH_SECONDS + 1
    await subject.key_ring(force=True)
    assert fake.jwks_calls == 2


async def test_a_forced_refresh_serves_the_previous_keys_while_the_interval_holds() -> None:
    """A token naming a new kid must not hard-fail while the cache says it is too early to look: the callback
    retries against the stale set, and the rotation lands on the next fetch."""
    fake = FakeIdP()
    clock = Clock()
    subject = idp(fake, clock)
    await subject.key_ring()
    fake.jwks = {"keys": [rsa_jwk("k2")]}
    held = await subject.key_ring(force=True)
    assert set(held.by_kid) == {"k1"}
    clock.t += JWKS_REFRESH_SECONDS + 1
    rotated = await subject.key_ring()
    assert set(rotated.by_kid) == {"k2"}


async def test_a_rotation_keeps_the_old_key_usable_until_the_next_fetch() -> None:
    """During a rotation an IdP lists both keys, and a token minted minutes ago may still name the old one.
    Holding the union is what keeps both sign-ins working."""
    fake = FakeIdP()
    clock = Clock()
    subject = idp(fake, clock)
    await subject.key_ring()
    clock.t += JWKS_REFRESH_SECONDS + 1
    fake.jwks = {"keys": [rsa_jwk("k1"), rsa_jwk("k2")]}
    both = await subject.key_ring()
    assert set(both.by_kid) == {"k1", "k2"}


async def test_a_jwks_failure_leaves_the_previous_keys_in_service() -> None:
    """A key rotation that lands while the JWKS host is briefly unreachable should not lock the tenant out for
    the whole interval."""
    fake = FakeIdP()
    clock = Clock()
    subject = idp(fake, clock)
    await subject.key_ring()
    clock.t += JWKS_REFRESH_SECONDS + 1
    fake.jwks_status = 503
    held = await subject.key_ring()
    assert set(held.by_kid) == {"k1"}


async def test_a_jwks_document_with_no_usable_key_leaves_the_previous_keys() -> None:
    fake = FakeIdP()
    clock = Clock()
    subject = idp(fake, clock)
    await subject.key_ring()
    clock.t += JWKS_REFRESH_SECONDS + 1
    fake.jwks = {"keys": [{"kty": "oct", "k": "x" * 32}]}
    held = await subject.key_ring()
    assert set(held.by_kid) == {"k1"}


async def test_a_first_jwks_failure_propagates_rather_than_inventing_a_key() -> None:
    fake = FakeIdP()
    fake.jwks_status = 503
    with pytest.raises(IdPUnreachable):
        await idp(fake).key_ring()


async def test_a_broken_jwks_host_is_not_hammered() -> None:
    """The retry interval applies to failed attempts too, or an outage turns into a flood."""
    fake = FakeIdP()
    fake.jwks_status = 503
    clock = Clock()
    subject = idp(fake, clock)
    for _ in range(5):
        with pytest.raises(IdPUnreachable):
            await subject.key_ring()
    assert fake.jwks_calls == 1


async def test_a_broken_discovery_document_is_not_reread_per_attempt() -> None:
    """Both upstream calls are throttled — a document that 500s must not be re-read once per sign-in either —
    and the next refresh interval gets a fresh chance rather than a permanent refusal."""
    fake = FakeIdP()
    fake.document_status = 500
    clock = Clock()
    subject = idp(fake, clock)
    for _ in range(5):
        with pytest.raises(IdPUnreachable):
            await subject.key_ring()
    assert fake.discovery_calls == 1
    fake.document_status = 200
    clock.t += JWKS_REFRESH_SECONDS + 1
    ring = await subject.key_ring()
    assert "k1" in ring.by_kid


async def test_a_failing_upstream_recovers_once_the_interval_passes() -> None:
    """The throttle bounds the traffic, it must not latch: an IdP that comes back is used again."""
    fake = FakeIdP()
    fake.document_status = 500
    clock = Clock()
    subject = idp(fake, clock)
    with pytest.raises(IdPUnreachable):
        await subject.key_ring()
    fake.document_status = 200
    clock.t += JWKS_REFRESH_SECONDS + 1
    assert "k1" in (await subject.key_ring()).by_kid
    clock.t += JWKS_REFRESH_SECONDS + 1
    fake.jwks = {"keys": [rsa_jwk("rotated")]}
    assert "rotated" in (await subject.key_ring()).by_kid


async def test_a_broken_jwks_host_does_not_retrigger_discovery() -> None:
    """Here the document did succeed, so the second clock is the one that should throttle: a key host that is
    down must not turn every sign-in into two upstream calls."""
    fake = FakeIdP()
    fake.jwks_status = 503
    clock = Clock()
    subject = idp(fake, clock)
    for _ in range(5):
        with pytest.raises(IdPUnreachable):
            await subject.key_ring()
    assert fake.discovery_calls == 1


async def test_two_issuers_do_not_share_keys_even_under_the_same_kid() -> None:
    """A deployment may federate more than one IdP, and `v1` is a plausible kid label at both. Keying the
    cache by issuer alone would verify one tenant's token against the other's key."""
    clock = Clock()
    acme = FakeIdP(jwks={"keys": [rsa_jwk("v1")]})
    other = FakeIdP(issuer=OTHER, jwks={"keys": [ec_jwk("v1")]})
    shared = KeySets()
    transport = multi(acme, other)
    ring_a = await IdP(ISSUER, transport=transport, now=clock, key_sets=shared).key_ring()
    ring_b = await IdP(OTHER, transport=transport, now=clock, key_sets=shared).key_ring()

    assert ring_a.key_for("v1") is not ring_b.key_for("v1")
    acme_token = jwt.encode({"sub": "u1"}, RSA, algorithm="RS256", headers={"kid": "v1"})
    PyJWS().decode_complete(acme_token, ring_a.key_for("v1"), algorithms=("RS256", "ES256"))
    other_token = jwt.encode({"sub": "u1"}, EC, algorithm="ES256", headers={"kid": "v1"})
    PyJWS().decode_complete(other_token, ring_b.key_for("v1"), algorithms=("RS256", "ES256"))


async def test_each_issuer_keeps_its_own_fetch_budget() -> None:
    """A second tenant's fetch must not make the first one's keys look current, or one busy IdP starves the
    other of its rotation."""
    clock = Clock()
    shared = KeySets()
    acme = FakeIdP()
    other = FakeIdP(issuer=OTHER, jwks={"keys": [ec_jwk("e1")]})
    transport = multi(acme, other)
    await IdP(ISSUER, transport=transport, now=clock, key_sets=shared).key_ring()
    await IdP(OTHER, transport=transport, now=clock, key_sets=shared).key_ring()
    clock.t += JWKS_REFRESH_SECONDS + 1
    assert shared.fresh(ISSUER, clock()) is None
    assert shared.may_fetch(ISSUER, clock()) is True


def test_a_key_retired_by_the_cap_leaves_the_ring() -> None:
    """A serve process runs for months and an IdP rotates on a schedule; without a bound the ring only grows.
    Retirement is safe because a token naming a retired kid forces a refetch rather than failing."""
    clock = Clock()
    shared = KeySets(max_keys=3)
    for i in range(3):
        shared.remember(ISSUER, KeyRing(by_kid={f"k{i}": object()}), clock())  # type: ignore[dict-item]
    held = shared.stale(ISSUER)
    assert held is not None and len(held.by_kid) == 3
    shared.remember(ISSUER, KeyRing(by_kid={"k3": object()}), clock())  # type: ignore[dict-item]
    after = shared.stale(ISSUER)
    assert after is not None
    assert len(after.by_kid) == 3
    assert "k0" not in after.by_kid
    assert "k3" in after.by_kid


async def test_a_retired_key_forces_a_refetch_rather_than_a_silent_miss() -> None:
    """The consequence of the cap, tested end to end: a token naming a retired kid is refused once, and the
    refresh that follows names it again."""
    fake = FakeIdP()
    clock = Clock()
    subject = IdP(ISSUER, transport=fake.transport(), now=clock, key_sets=KeySets(max_keys=1))
    await subject.key_ring()
    clock.t += JWKS_REFRESH_SECONDS + 1
    fake.jwks = {"keys": [rsa_jwk("k2")]}
    rotated = await subject.key_ring()
    assert set(rotated.by_kid) == {"k2"}
    assert "k1" not in rotated.by_kid


def test_key_sets_forget_drops_the_keys_and_the_retry_clock() -> None:
    shared = KeySets()
    clock = Clock()
    shared.remember(ISSUER, KeyRing(by_kid={"k1": object()}), clock())  # type: ignore[dict-item]
    assert shared.may_fetch(ISSUER, clock()) is False
    shared.forget(ISSUER)
    assert shared.fresh(ISSUER, clock()) is None
    assert shared.stale(ISSUER) is None
    assert shared.may_fetch(ISSUER, clock()) is True


def test_forgetting_one_issuer_leaves_the_other_intact() -> None:
    shared = KeySets()
    clock = Clock()
    shared.remember(ISSUER, KeyRing(by_kid={"v1": "a"}), clock())
    shared.remember(OTHER, KeyRing(by_kid={"v1": "b"}), clock())
    shared.forget(ISSUER)
    assert shared.stale(ISSUER) is None
    held = shared.stale(OTHER)
    assert held is not None and held.by_kid == {"v1": "b"}


# --- the token exchange -------------------------------------------------------------------------------


async def test_the_exchange_posts_a_form_to_the_documented_token_endpoint() -> None:
    fake = FakeIdP()
    body = await idp(fake).exchange({"grant_type": "authorization_code", "code": "c1", "client_secret": "s"})
    assert body["id_token"] == "a.jwt"
    assert fake.token_bodies[0]["code"] == "c1"
    assert fake.token_bodies[0]["grant_type"] == "authorization_code"


async def test_the_exchange_goes_to_the_documented_endpoint_not_the_issuer_root() -> None:
    """A tenant issuer publishes its token endpoint under its own path; posting to the origin would be a
    different endpoint that happens to exist."""
    fake = FakeIdP(
        document=FakeIdP(issuer="https://id.acme.com/t/7").document_for(
            token_endpoint="https://id.acme.com/t/7/oauth/token"
        )
    )
    seen: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        if str(request.url) == "https://id.acme.com/t/7/oauth/token":
            return httpx.Response(200, json={"id_token": "x"})
        return fake.handle(request)

    subject = IdP("https://id.acme.com/t/7", transport=httpx.MockTransport(route), now=Clock())
    body = await subject.exchange({"code": "c"})
    assert body["id_token"] == "x"
    assert "POST https://id.acme.com/t/7/oauth/token" in seen


async def test_an_error_from_the_idp_never_quotes_what_we_posted() -> None:
    """IdPs echo the request into error_description. The same discipline sources/github.py applies: mask
    everything we sent, keep the IdP's own words."""
    fake = FakeIdP()
    fake.token_reply = {
        "error": "invalid_request",
        "error_description": "code c1 and secret s3cret and verifier v9 and state st1 are wrong",
    }
    body = await idp(fake).exchange(
        {"code": "c1", "client_secret": "s3cret", "code_verifier": "v9", "state": "st1"}
    )
    said = body["error_description"]
    for secret in ("s3cret", "c1", "v9", "st1"):
        assert secret not in said
    assert body["error"] == "invalid_request"


async def test_the_masking_is_not_a_name_whitelist() -> None:
    """A field added to the exchange form later must be masked by the same rule, not missed because nobody
    remembered to list it."""
    fake = FakeIdP()
    fake.token_reply = {"error_description": "bad assertion ZZtailZ"}
    body = await idp(fake).exchange({"client_assertion": "ZZtailZ", "code": "c1"})
    assert "ZZtailZ" not in body["error_description"]


async def test_an_error_without_a_description_is_left_alone() -> None:
    fake = FakeIdP()
    fake.token_reply = {"error": "invalid_grant"}
    body = await idp(fake).exchange({"code": "c1"})
    assert body["error"] == "invalid_grant"
    assert "error_description" not in body


async def test_a_non_string_description_is_not_masked_as_text() -> None:
    """Nothing to mask in a non-string, so it must not be stringified into something a caller might print; the
    caller only ever reads the typed `error` key."""
    fake = FakeIdP()
    fake.token_reply = {"error_description": {"nested": "s3cret"}}
    body = await idp(fake).exchange({"client_secret": "s3cret"})
    assert body["error_description"] == {"nested": "s3cret"}


async def test_a_token_reply_that_is_not_json_is_refused() -> None:
    fake = FakeIdP()
    fake.token_reply = "<html>gateway timeout</html>"
    with pytest.raises(IdPUnreachable):
        await idp(fake).exchange({"code": "c1"})


class RaisingTransport(httpx.AsyncBaseTransport):
    """Fails the way a network does. A MockTransport can only answer, so this is the object that proves a
    transport error becomes a typed refusal instead of an httpx traceback escaping the route."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


async def test_a_transport_failure_during_the_exchange_is_refused() -> None:
    """Discovery is handed over already cached, so the request that dies is the token POST."""
    fake = FakeIdP()
    docs = ProviderDocs()
    await IdP(ISSUER, transport=fake.transport(), now=Clock(), docs=docs).provider()
    subject = IdP(ISSUER, transport=RaisingTransport(), now=Clock(), docs=docs)
    with pytest.raises(IdPUnreachable):
        await subject.exchange({"code": "c1"})


async def test_a_transport_failure_fetching_keys_is_refused() -> None:
    subject = IdP(ISSUER, transport=RaisingTransport(), now=Clock())
    with pytest.raises(IdPUnreachable):
        await subject.key_ring()


# --- caches shared across instances -------------------------------------------------------------------


async def test_a_shared_key_set_is_not_refetched_by_a_second_client() -> None:
    """One KeySets per serve process, several IdP objects over its lifetime: the budget belongs to the
    deployment, not the object."""
    fake = FakeIdP()
    clock = Clock()
    shared = KeySets()
    await IdP(ISSUER, transport=fake.transport(), now=clock, key_sets=shared).key_ring()
    await IdP(ISSUER, transport=fake.transport(), now=clock, key_sets=shared).key_ring()
    assert fake.jwks_calls == 1


async def test_a_shared_document_cache_is_not_reread_by_a_second_client() -> None:
    fake = FakeIdP()
    clock = Clock()
    docs = ProviderDocs()
    await IdP(ISSUER, transport=fake.transport(), now=clock, docs=docs).provider()
    await IdP(ISSUER, transport=fake.transport(), now=clock, docs=docs).provider()
    assert fake.discovery_calls == 1


async def test_two_clients_without_shared_caches_do_not_share_a_fetch_budget() -> None:
    """The default must be per instance. A class-level default would leak one object's fetch into another's
    freshness decision, which reads as a cache that never expires."""
    fake = FakeIdP()
    clock = Clock()
    await idp(fake, clock).key_ring()
    await idp(fake, clock).key_ring()
    assert fake.jwks_calls == 2
