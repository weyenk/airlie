"""Verifying a federated ID token (auth.oidc; see the spec at
docs/superpowers/specs/2026-11-26-oidc-sign-in-design.md).

This is the one place an IdP's assertion is checked, so it is deliberately boring and total: a token, a
caller-supplied key ring, and the current time come in, and either typed claims come out or a TokenRejected
naming a code. Nothing is fetched and no clock is read here, which is what lets the tests generate their own
keys and mint every rejection on purpose.

`now` is a parameter rather than a `time.time()` call for the same reason: an expiry boundary, a future `iat`,
and a clock skew are the interesting cases, and they are unreachable if the function owns the clock.

A refusal carries a code and never a claim. What an IdP echoes, or what a token holds, is not ours to repeat
into an error body: the person at the browser already has the token, and an error that quotes it leaks a
subject or an address into logs and history for no benefit."""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from jwt.api_jws import PyJWS

#: What we will verify. Restricted deliberately: `alg` is attacker-chosen in the header, so a set wide
#: enough to include HS256 turns the IdP's public key into a shared secret an attacker can sign with.
SUPPORTED_ALGORITHMS: tuple[str, ...] = ("RS256", "ES256")

#: Tolerance for a token whose iat is very slightly ahead of our clock; IdP and server clocks are never exact.
ISSUED_SKEW_SECONDS = 30


class TokenRejected(Exception):
    """The token is not a sign-in. `code` is what we log and what the caller may say; the message carries no
    claim, no token and no IdP text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class KeyRing:
    """The IdP's public keys by `kid` — the result of a JWKS lookup, supplied by the caller so verification
    itself stays pure and testable. Built once per request from a cached key set (Slice B)."""

    by_kid: dict[str, str]

    def key_for(self, kid: str) -> str:
        if kid not in self.by_kid:
            raise TokenRejected("unknown_kid")
        return self.by_kid[kid]


@dataclass(frozen=True)
class Claims:
    """The verified assertions, in the shape the callback needs. `sub` is the identity; email is display data
    and never a key (see the spec's "no auto-link by email")."""

    iss: str
    sub: str
    email: str | None
    groups: tuple[str, ...] = ()
    admin: bool = False
    auth_time: int | None = None
    sid: str | None = None


def _claim(payload: dict[str, object], name: str, code: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TokenRejected(code)
    return value


def _expect(payload: dict[str, object], name: str, want: str, code: str) -> None:
    """A claim that must equal a value we chose. Mismatch and absence get the same refusal, because a token
    that omits `iss` asserts nothing we can trust."""
    if _claim(payload, name, code) != want:
        raise TokenRejected(code)


def _expect_audience(payload: dict[str, object], audience: str) -> None:
    """`aud` is a string or, per the spec, a list. A list we are merely one of is valid; a list that does not
    name us is not. A multi-audience token is where `azp` would come in (Slice B, if an IdP sends one)."""
    value = payload.get("aud")
    if isinstance(value, str):
        if value != audience:
            raise TokenRejected("audience")
        return
    if isinstance(value, (list, tuple)) and audience in [v for v in value if isinstance(v, str)]:
        return
    raise TokenRejected("audience")


def _expect_expiry(payload: dict[str, object], now: float) -> None:
    """`exp` and `iat` against the caller's clock, not pyjwt's — the reason `now` is a parameter at all."""
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise TokenRejected("expired")
    if exp <= now:
        raise TokenRejected("expired")
    iat = payload.get("iat")
    if iat is None:
        raise TokenRejected("issued_at")
    if not isinstance(iat, (int, float)):
        raise TokenRejected("issued_at")
    if iat > now + ISSUED_SKEW_SECONDS:
        raise TokenRejected("issued_at")
    nbf = payload.get("nbf")
    if isinstance(nbf, (int, float)) and nbf > now + ISSUED_SKEW_SECONDS:
        raise TokenRejected("not_before")


def _string_list(payload: dict[str, object], name: str) -> tuple[str, ...]:
    """A claim that carries several values, tolerant of the single-value form: some IdPs send one group as a
    bare string. Iterating a string as a sequence would split it into letters matching no configured group."""
    value = payload.get(name)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def verify_id_token(
    token: str,
    keys: KeyRing,
    *,
    issuer: str,
    audience: str,
    nonce: str,
    now: float,
    allowed_domains: tuple[str, ...] = (),
    groups_claim: str | None = None,
    allowed_groups: tuple[str, ...] = (),
    admin_role_claim: str | None = None,
) -> Claims:
    """Verify `token` against `keys` and the expected issuer, audience and nonce, and return its claims.

    Order matters: signature first, because every later check reads a payload that means nothing until it is
    authenticated. Then the OIDC assertions (iss, aud, exp, iat, nonce, sub), then airlie's own gates. Raises
    TokenRejected naming the first check that failed.
    """

    header = _unverified_header(token)
    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm not in SUPPORTED_ALGORITHMS:
        raise TokenRejected("algorithm")

    kid = header.get("kid")
    if not isinstance(kid, str):
        raise TokenRejected("unknown_kid")
    key = keys.key_for(kid)

    # 1. Authenticate. PyJWS checks the signature and the algorithm allowlist and reads no claim and no clock,
    # so nothing below is evaluated against a payload that is not yet trusted. (jwt.decode is deliberately not
    # used for this: it validates exp/iat against its own time.time(), which would make `now` a lie and
    # put the expiry boundary out of reach of a test.)
    try:
        PyJWS().decode_complete(token, key, algorithms=SUPPORTED_ALGORITHMS)
    except jwt.PyJWTError as e:  # a JWS failure means "not signed by that key"; nothing here is worth quoting
        raise TokenRejected("bad_signature") from e

    # 2. Read the payload without verifying anything, now that the signature above vouches for these bytes.
    # The key is passed only because the signature is not verified here rather than because it is needed.
    try:
        payload: dict[str, object] = jwt.decode(token, key, options={"verify_signature": False}, verify=False)
    except jwt.PyJWTError as e:
        raise TokenRejected("malformed") from e

    # 3. The assertions, each against the caller's `now`.
    _expect(payload, "iss", issuer, "issuer")
    _expect_audience(payload, audience)
    _expect_expiry(payload, now)
    token_nonce = payload.get("nonce")
    if not isinstance(token_nonce, str) or token_nonce != nonce:
        raise TokenRejected("nonce")
    sub = _claim(payload, "sub", "subject")

    email_value = payload.get("email")
    email = email_value if isinstance(email_value, str) and email_value else None

    if allowed_domains:
        if email is None:
            # A gate we cannot evaluate is not a gate that passed. A profile without an email would otherwise
            # sail through a domain restriction that reads, in the config, as being enforced.
            raise TokenRejected("email")
        domain = email.rpartition("@")[2].lower()
        if domain not in {d.lower() for d in allowed_domains}:
            raise TokenRejected("domain")

    groups = _string_list(payload, groups_claim) if groups_claim else ()
    if allowed_groups:
        wanted = {g.lower() for g in allowed_groups}
        if not wanted.intersection(g.lower() for g in groups):
            raise TokenRejected("group")

    admin = bool(admin_role_claim) and _has_value(payload, admin_role_claim or "")

    auth_time = payload.get("auth_time")
    sid_value = payload.get("sid")

    return Claims(
        iss=issuer,
        sub=sub,
        email=email,
        groups=groups,
        admin=admin,
        auth_time=int(auth_time) if isinstance(auth_time, (int, float)) else None,
        sid=sid_value if isinstance(sid_value, str) else None,
    )


def _has_value(payload: dict[str, object], name: str) -> bool:
    """Whether a claim names one of our configured admin groups. Only ever called with a configured claim
    name, so an unset admin_role_claim cannot grant anything."""
    value = payload.get(name)
    if isinstance(value, str):
        return bool(value)
    return isinstance(value, (list, tuple)) and bool(value)


def _unverified_header(token: str) -> dict[str, object]:
    """The header alone, to choose a key and reject a bad `alg` before paying for verification. Untrusted by
    definition — it is read only to decide *how* to verify, never what to grant."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise TokenRejected("malformed") from e
    return header if isinstance(header, dict) else {}
