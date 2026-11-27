"""The OIDC authorization attempts in flight, in this process: one browser interaction that asked for a login,
and the four values that make the callback answerable only by the browser that started it.

state is CSRF protection and nonce is token-injection protection. They are different defects, so both are
present and neither is reused, and PKCE's verifier is here too because the IdP will not accept it twice. An
attempt is consumed on the first callback: a code is single-use, so a second presentation of the same one is a
replay and not a double click.

Mirrors the shape of auth.api.Ceremonies, which caps outstanding WebAuthn challenges for the same reason —
/start is public, so an unbounded dict is a way for a stranger to grow a serve process."""

from __future__ import annotations

import dataclasses

import pytest

from airlie.auth.oidc_flow import Attempts, Pending

T0 = 1_000_000.0
TTL = 300.0


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


def pending(clock: Clock, **overrides: object) -> Pending:
    """An attempt open *now*, as the real /start builds one — expires_at is derived from the live clock so a
    test that advances time first still opens a live attempt."""
    base = {
        "state": "state-abc",
        "nonce": "nonce-abc",
        "verifier": "verifier-abc",
        "redirect_uri": "https://airlie.acme.com/v1/auth/oidc/callback",
        "expires_at": clock() + TTL,
    }
    base.update(overrides)
    return Pending(**base)  # type: ignore[arg-type]


def test_an_open_attempt_is_taken_by_its_own_state() -> None:
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock))
    got = attempts.take("state-abc")
    assert got is not None
    assert got.nonce == "nonce-abc"
    assert got.verifier == "verifier-abc"
    assert got.redirect_uri == "https://airlie.acme.com/v1/auth/oidc/callback"


def test_an_attempt_is_consumed_once() -> None:
    """A second callback with the same state is a replay, not a browser refreshing."""
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock))
    assert attempts.take("state-abc") is not None
    assert attempts.take("state-abc") is None


def test_an_unknown_state_answers_nothing() -> None:
    attempts = Attempts(Clock(), ttl_seconds=TTL)
    assert attempts.take("state-who") is None


def test_an_expired_attempt_is_refused_and_forgotten() -> None:
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock))
    clock.t = T0 + TTL + 1
    assert attempts.take("state-abc") is None
    # forgotten, not merely past: reopening the same state cannot resurrect it
    attempts.open(pending(clock, state="state-abc"))
    assert attempts.take("state-abc") is not None


def test_an_attempt_at_exactly_its_expiry_is_refused() -> None:
    """The boundary, reachable only because the clock is injected."""
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock))
    clock.t = T0 + TTL
    assert attempts.take("state-abc") is None


def test_an_attempt_one_tick_before_expiry_is_still_good() -> None:
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock))
    clock.t = T0 + TTL - 0.001
    assert attempts.take("state-abc") is not None


def test_a_second_attempt_replaces_the_first_for_this_state_only() -> None:
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock))
    attempts.open(pending(clock, state="state-def", nonce="nonce-def"))
    assert attempts.take("state-def") is not None
    assert attempts.take("state-abc") is not None


def test_the_cap_holds_and_the_oldest_go_first() -> None:
    """/start is public: without a cap a stranger could grow this process without limit."""
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL, max_pending=3)
    for i in range(3):
        attempts.open(pending(clock, state=f"s{i}"))
    attempts.open(pending(clock, state="s3"))
    assert len(attempts) == 3
    assert attempts.take("s0") is None  # the oldest was dropped
    assert attempts.take("s3") is not None


def test_the_cap_is_measured_not_guessed() -> None:
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL, max_pending=2)
    assert len(attempts) == 0
    attempts.open(pending(clock, state="a"))
    assert len(attempts) == 1
    attempts.open(pending(clock, state="b"))
    assert len(attempts) == 2
    attempts.open(pending(clock, state="c"))
    assert len(attempts) == 2


def test_expired_entries_are_swept_on_open() -> None:
    """Otherwise a person who starts a sign-in and walks away leaves the entry until the cap evicts it, and a
    flood of abandoned attempts fills the store with dead entries rather than live ones."""
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL, max_pending=10)
    for i in range(4):
        attempts.open(pending(clock, state=f"s{i}"))
    clock.t = T0 + TTL + 1
    attempts.open(pending(clock, state="fresh"))
    assert len(attempts) == 1
    assert attempts.take("fresh") is not None


def test_a_state_starts_with_no_user() -> None:
    """The person is only known after the ID token is verified; nothing here may pre-claim who is
    signing in."""
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock))
    got = attempts.take("state-abc")
    assert got is not None
    assert not hasattr(got, "user_id")


def test_a_replaced_attempt_cannot_be_polled_twice() -> None:
    """Two tabs, one person: the second /start wins, and the first callback must not complete on the old
    verifier."""
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock, state="same", verifier="verifier-one"))
    attempts.open(pending(clock, state="same", verifier="verifier-two"))
    got = attempts.take("same")
    assert got is not None
    assert got.verifier == "verifier-two"


def test_an_empty_state_is_not_a_key_anything_matches() -> None:
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock, state=""))
    assert attempts.take("") is not None
    assert attempts.take("anything") is None


def test_closing_an_attempt_ends_it_without_consuming_another() -> None:
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock, state="a"))
    attempts.open(pending(clock, state="b"))
    attempts.close("a")
    assert attempts.take("a") is None
    assert attempts.take("b") is not None


def test_closing_a_state_that_is_not_there_changes_nothing() -> None:
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock, state="a"))
    attempts.close("nope")
    assert attempts.take("a") is not None


def test_two_attempts_never_share_a_verifier_or_nonce() -> None:
    """The values come from the caller, so this test holds the store honest about keeping them apart."""
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=TTL)
    attempts.open(pending(clock, state="one", nonce="n1", verifier="v1"))
    attempts.open(pending(clock, state="two", nonce="n2", verifier="v2"))
    first = attempts.take("one")
    second = attempts.take("two")
    assert first is not None and second is not None
    assert (first.nonce, first.verifier) == ("n1", "v1")
    assert (second.nonce, second.verifier) == ("n2", "v2")


def test_a_ttl_of_zero_refuses_immediately() -> None:
    """Not a configuration anyone should ship, but it must fail closed rather than open."""
    clock = Clock()
    attempts = Attempts(clock, ttl_seconds=0.0)
    attempts.open(pending(clock, expires_at=clock()))
    assert attempts.take("state-abc") is None


def test_pending_is_immutable() -> None:
    """A stored attempt whose nonce could be rewritten after the fact would defeat the nonce check."""
    p = pending(Clock())
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.nonce = "rewritten"  # type: ignore[misc]
