"""ID-token verification: the one place a federated assertion is checked, so the one thing here that must
not be wrong. Pure by construction — it takes the token, a caller-supplied key ring and the clock, and
fetches nothing — which is why the tests can generate their own keys and mint every rejection on purpose.

Every refusal names a code rather than IdP detail, because what an IdP says in an error is not ours to
repeat."""

from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from airlie.auth.oidc import Claims, KeyRing, TokenRejected, verify_id_token

ISS = "https://acme.okta.com"
AUD = "0oa123"
NONCE = "nonce-abc"
NOW = 1_800_000_000

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_pem(key: rsa.RSAPrivateKey) -> str:
    """The public half as PEM — what a JWKS entry would be turned into for verification."""
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def ring(*keys: rsa.RSAPrivateKey, kid: str = "k1") -> KeyRing:
    """The kid the token header carries, mapped to its public key — what a JWKS lookup would hand us."""
    return KeyRing({kid: public_pem(keys[0])})


def token(
    claims: dict | None = None,
    *,
    key: rsa.RSAPrivateKey = KEY,
    kid: str = "k1",
    alg: str = "RS256",
) -> str:
    body = {
        "iss": ISS,
        "aud": AUD,
        "sub": "user-1",
        "exp": NOW + 3600,
        "iat": NOW,
        "auth_time": NOW,
        "nonce": NONCE,
        "email": "noah@acme.com",
        **(claims or {}),
    }
    return jwt.encode(body, key, algorithm=alg, headers={"kid": kid})


def check(jwt_text: str, **overrides) -> Claims:
    kwargs = {"issuer": ISS, "audience": AUD, "nonce": NONCE, "now": NOW, "allowed_domains": ()}
    kwargs.update(overrides)
    return verify_id_token(jwt_text, ring(KEY), **kwargs)


def test_a_well_formed_token_gives_claims() -> None:
    claims = check(token())
    assert claims.sub == "user-1"
    assert claims.email == "noah@acme.com"
    assert claims.iss == ISS


def test_an_unknown_kid_is_refused() -> None:
    with pytest.raises(TokenRejected, match="unknown_kid"):
        check(token(kid="k9"))


def test_a_token_signed_by_another_key_is_refused() -> None:
    """The kid matches, the signature does not: a key the IdP does not have."""
    with pytest.raises(TokenRejected, match="bad_signature"):
        check(token(key=OTHER))


def test_an_untrusted_issuer_is_refused() -> None:
    """The classic confusion: a token from an issuer we do not trust, carrying a real-looking sub."""
    with pytest.raises(TokenRejected, match="issuer"):
        check(token({"iss": "https://evil.example"}))


def test_a_token_audience_for_another_client_is_refused() -> None:
    with pytest.raises(TokenRejected, match="audience"):
        check(token({"aud": "some-other-app"}))


def test_an_expired_token_is_refused() -> None:
    with pytest.raises(TokenRejected, match="expired"):
        check(token({"exp": NOW - 1}))


def test_a_nonce_that_does_not_match_is_refused() -> None:
    """A captured token relayed to a different browser interaction has the right shape and the wrong nonce."""
    with pytest.raises(TokenRejected, match="nonce"):
        check(token(), nonce="nonce-different")


def test_an_absent_nonce_is_refused() -> None:
    with pytest.raises(TokenRejected, match="nonce"):
        check(token({"nonce": None}))


def test_a_missing_subject_is_refused() -> None:
    with pytest.raises(TokenRejected, match="subject"):
        check(token({"sub": None}))


def test_a_token_minted_in_the_future_is_refused() -> None:
    with pytest.raises(TokenRejected, match="issued_at"):
        check(token({"iat": NOW + 600}))


def test_an_unlisted_algorithm_is_refused() -> None:
    """alg is attacker-chosen in the header. If HS256 were accepted, an attacker who knows a shared secret (or
    picks one themselves, since they build the token) could sign something claiming any `sub`, and the kid
    would still name a real key. pyjwt refuses to even encode an RSA public key as an HMAC secret, so the
    threat is modelled with a secret the attacker chose — the bypass is the algorithm header, not the key."""
    attacker = jwt.encode(
        {"iss": ISS, "aud": AUD, "sub": "someone-else", "exp": NOW + 3600, "iat": NOW, "nonce": NONCE},
        "a-secret-the-attacker-picked-which-is-long-enough-for-hs256",
        algorithm="HS256",
        headers={"kid": "k1"},  # names a key we do have, so only the algorithm stands between us
    )
    with pytest.raises(TokenRejected, match="algorithm"):
        check(attacker)


def test_none_alg_is_refused() -> None:
    """The other half of the same bypass: an unsigned token. Crafted by hand because no library will mint one
    for us; it must still be refused, and by the algorithm gate rather than a key lookup."""
    unsigned = _unsigned_token(
        {"iss": ISS, "aud": AUD, "sub": "u", "exp": NOW + 60, "iat": NOW, "nonce": NONCE}
    )
    assert jwt.get_unverified_header(unsigned)["alg"] == "none"
    with pytest.raises(TokenRejected, match="algorithm"):
        check(unsigned)


def _unsigned_token(claims: dict) -> str:
    """A JWS whose header says alg "none" and whose signature is empty — what an attacker sends, not what any
    honest IdP would. Built here so the refusal has something real to reject."""

    def b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."


def test_an_email_outside_the_allowed_domains_is_refused() -> None:
    with pytest.raises(TokenRejected, match="domain"):
        check(token({"email": "noah@other.com"}), allowed_domains=("acme.com",))


def test_an_allowed_domain_passes() -> None:
    assert check(token(), allowed_domains=("acme.com",)).email == "noah@acme.com"


def test_the_domain_check_is_not_case_tricky() -> None:
    """ACME.COM and acme.com are the same domain; a mismatch there would lock people out at sign-in."""
    assert check(token({"email": "noah@ACME.com"}), allowed_domains=("acme.com",)).sub == "user-1"


def test_a_gate_needing_email_refuses_when_the_claim_is_absent() -> None:
    """A profile without an email cannot satisfy a domain gate, and allowing it would read as the gate
    working while gating nothing."""
    with pytest.raises(TokenRejected, match="email"):
        check(token({"email": None}), allowed_domains=("acme.com",))


def test_groups_are_read_from_the_named_claim() -> None:
    claims = check(token({"groups": ["airlie-users", "ops"]}), groups_claim="groups")
    assert claims.groups == ("airlie-users", "ops")


def test_a_group_gate_refuses_a_token_without_the_claim() -> None:
    with pytest.raises(TokenRejected, match="group"):
        check(token(), groups_claim="groups", allowed_groups=("airlie-users",))


def test_a_group_gate_refuses_a_token_without_the_right_group() -> None:
    with pytest.raises(TokenRejected, match="group"):
        check(
            token({"groups": ["everyone"]}),
            groups_claim="groups",
            allowed_groups=("airlie-users",),
        )


def test_a_group_gate_accepts_a_member() -> None:
    claims = check(
        token({"groups": "airlie-users"}),  # some IdPs send one value, not a list
        groups_claim="groups",
        allowed_groups=("airlie-users",),
    )
    assert claims.groups == ("airlie-users",)


def test_a_single_group_claim_is_not_split_into_letters() -> None:
    """A bare string is one group. Iterating it as a sequence would turn 'airlie-users' into a bag of letters
    that matches no configured group."""
    claims = check(token({"groups": "airlie-users"}), groups_claim="groups")
    assert claims.groups == ("airlie-users",)
    assert "a" not in claims.groups


def test_the_admin_claim_is_only_read_when_configured() -> None:
    assert check(token({"groups": ["admins"]})).admin is False
    assert check(token({"groups": ["admins"]}), admin_role_claim="groups").admin is True


def test_a_rejection_says_nothing_about_the_token() -> None:
    """The refusal must not echo claims or the IdP's text into an error body."""
    secret = token({"sub": "victim-12345", "email": "victim@acme.com"})
    with pytest.raises(TokenRejected) as got:
        check(secret, issuer="https://evil.example")
    assert "victim" not in str(got.value)
    assert got.value.code == "issuer"


def test_iat_within_the_future_skew_still_passes() -> None:
    assert check(token({"iat": NOW + 5})).sub == "user-1"


def test_the_current_time_is_supplied_not_read_from_the_clock() -> None:
    """A verification function that reads time.time() itself cannot be tested at an expiry boundary; a NOW
    inside the token's window proves the caller owns the clock."""
    assert check(token({"exp": NOW + 30}), now=NOW).sub == "user-1"
    assert time.time() != NOW
