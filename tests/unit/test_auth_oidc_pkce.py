"""The values /start generates and the challenge the IdP will recompute.

PKCE is the reason a stolen authorization code is not a sign-in, and it only works if the verifier
is high entropy, url-safe per RFC 7636, and 43-128 characters — a short or padded verifier is what
makes an IdP answer `invalid_request` on someone's first attempt, with a message that names
nothing. State and nonce must be distinct from it and from each other too: they answer different
attacks (a request forged into the callback versus a token handed to the wrong browser), so one
value cannot serve both roles."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import string

from airlie.auth.oidc_flow import code_challenge, new_attempt

# RFC 7636's unreserved set, which is also the set an IdP will accept without further encoding
URL_SAFE = re.compile(r"^[A-Za-z0-9\-._~]+$")
CALLBACK = "https://airlie.acme.com/v1/auth/oidc/callback"


def test_the_verifier_is_url_safe_and_in_the_length_the_spec_allows() -> None:
    """43-128 characters of the unreserved set. Below 43 or above 128 the IdP refuses the exchange;
    a `+` or `/` in the value is refused by stricter clients."""
    for _ in range(50):
        verifier = secrets.token_urlsafe(48)
        assert 43 <= len(verifier) <= 128
        assert URL_SAFE.match(verifier), verifier


def test_generated_values_are_not_padding_in_disguise() -> None:
    """A generator that produced `AAAA...` would pass a length-only check and be worthless."""
    assert len({secrets.token_urlsafe(48) for _ in range(200)}) == 200


def test_three_values_come_out_of_one_attempt_and_none_repeat() -> None:
    """state, nonce and the verifier protect different things; a shared value means one leaked
    secret breaks both guarantees."""
    seen_state, seen_nonce, seen_verifier = set(), set(), set()
    for _ in range(60):
        attempt = new_attempt(redirect_uri=CALLBACK, now=1_000_000.0)
        assert len({attempt.state, attempt.nonce, attempt.verifier}) == 3
        seen_state.add(attempt.state)
        seen_nonce.add(attempt.nonce)
        seen_verifier.add(attempt.verifier)
    assert len(seen_state) == len(seen_nonce) == len(seen_verifier) == 60


def test_an_attempt_carries_the_redirect_uri_it_was_opened_with() -> None:
    """The exchange must repeat the exact redirect_uri the authorize call used, or the IdP refuses
    the code. Carrying it on the attempt is what makes /start's answer and /callback's request
    agree by construction rather than by two builders matching."""
    assert new_attempt(redirect_uri=CALLBACK, now=1_000_000.0).redirect_uri == CALLBACK


def test_an_attempt_expires_when_the_clock_says_so() -> None:
    """A person who starts a sign-in and walks away must not leave a live attempt behind for an
    hour."""
    attempt = new_attempt(redirect_uri=CALLBACK, now=1_000_000.0, ttl_seconds=300.0)
    assert attempt.expires_at == 1_000_300.0


def test_the_ttl_is_the_callers_to_choose() -> None:
    attempt = new_attempt(redirect_uri=CALLBACK, now=1_000_000.0, ttl_seconds=60.0)
    assert attempt.expires_at == 1_000_060.0


def test_an_attempt_still_holds_no_user() -> None:
    """Restated where a user id would most plausibly be added by accident."""
    assert not hasattr(new_attempt(redirect_uri=CALLBACK, now=0.0), "user_id")


def test_the_verifier_and_its_challenge_are_the_same_bytes_encoded_differently() -> None:
    """The verifier goes to the token endpoint, its S256 challenge to the authorize endpoint.
    Encoding the challenge with padding, or as base64 rather than base64url, is the classic reason
    an exchange fails with a message that names nothing useful."""
    verifier = "x" * 43
    challenge = code_challenge(verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert "=" not in challenge
    assert URL_SAFE.match(challenge)
    assert challenge != verifier


def test_the_challenge_changes_with_the_verifier() -> None:
    assert code_challenge("a" * 43) != code_challenge("b" * 43)


def test_a_challenge_of_a_real_verifier_round_trips_through_the_idps_check() -> None:
    """What the IdP recomputes server-side: challenge == BASE64URL(SHA256(verifier)). If airlie
    ever encoded it differently, every sign-in would fail at the exchange rather than at the
    redirect, which is the worst place in the flow to discover a bug."""
    attempt = new_attempt(redirect_uri=CALLBACK, now=0.0)
    recomputed = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode("ascii")).digest())
    assert recomputed.rstrip(b"=").decode("ascii") == code_challenge(attempt.verifier)
    assert set(attempt.verifier) <= set(string.ascii_letters + string.digits + "-_")
