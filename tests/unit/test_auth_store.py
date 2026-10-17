"""The auth store: users, invites, passkey credentials and sessions in one SQLite file. Tokens are hashed at
rest; deleting a user takes their credentials and sessions with them."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from airlie.auth.store import AuthStore, Credential


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    s = AuthStore(tmp_path / "auth.sqlite", now=clock)
    yield s
    s.close()


def test_add_user_returns_it_and_it_is_found_by_id_login_and_in_the_list(
    store: AuthStore, clock: Clock
) -> None:
    u = store.add_user("Noah", "admin", login="noah@github")
    assert u.name == "Noah" and u.role == "admin" and u.login == "noah@github"
    assert u.created_at == clock.t
    assert store.user(u.id) == u
    assert store.user_by_login("noah@github") == u
    assert store.user_by_login("nobody") is None
    assert store.user("ghost") is None
    other = store.add_user("Pat", "member")
    assert other.login is None
    assert store.users() == [u, other]


def test_the_store_is_a_wal_sqlite_file_that_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "auth.sqlite"
    s = AuthStore(path)
    u = s.add_user("Noah", "admin")
    assert s.conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    s.close()
    again = AuthStore(path)
    assert again.user(u.id) == u
    again.close()


def test_an_invite_is_consumed_once(store: AuthStore) -> None:
    u = store.add_user("Pat", "member")
    token = store.make_invite(u.id, hours=24)
    assert len(token) >= 32
    assert store.take_invite(token) == u
    assert store.take_invite(token) is None
    assert store.take_invite("never-issued") is None


def test_an_expired_invite_is_none(store: AuthStore, clock: Clock) -> None:
    u = store.add_user("Pat", "member")
    token = store.make_invite(u.id, hours=1)
    clock.t += 3601
    assert store.take_invite(token) is None


def test_invite_and_session_tokens_are_not_stored_in_clear(store: AuthStore, tmp_path: Path) -> None:
    u = store.add_user("Pat", "member")
    invite = store.make_invite(u.id, hours=24)
    session = store.make_session(u.id, days=30)
    raw = sqlite3.connect(tmp_path / "auth.sqlite")
    dump = "\n".join(raw.iterdump())
    raw.close()
    assert invite not in dump
    assert session not in dump


def test_credentials_are_kept_per_user_and_the_sign_count_bumps(store: AuthStore, clock: Clock) -> None:
    u = store.add_user("Pat", "member")
    cred = Credential(
        id=b"\x01\x02",
        user_id=u.id,
        public_key=b"pk",
        sign_count=3,
        transports=("internal",),
        created_at=clock.t,
    )
    store.add_credential(cred)
    assert store.credentials(u.id) == [cred]
    assert store.credential(b"\x01\x02") == cred
    assert store.credential(b"\x09") is None
    store.bump_sign_count(b"\x01\x02", 7)
    got = store.credential(b"\x01\x02")
    assert got is not None and got.sign_count == 7


def test_sessions_resolve_to_their_user_until_ended_or_expired(store: AuthStore, clock: Clock) -> None:
    u = store.add_user("Pat", "member")
    token = store.make_session(u.id, days=30)
    assert store.session_user(token) == u
    assert store.session_user("bogus") is None
    clock.t += 31 * 86400
    assert store.session_user(token) is None
    clock.t -= 31 * 86400
    assert store.session_user(token) == u
    store.end_sessions(u.id)
    assert store.session_user(token) is None


def test_deleting_a_user_cascades_to_credentials_sessions_and_invites(store: AuthStore, clock: Clock) -> None:
    u = store.add_user("Pat", "member")
    store.add_credential(Credential(b"c", u.id, b"pk", 0, (), clock.t))
    session = store.make_session(u.id, days=30)
    invite = store.make_invite(u.id, hours=24)
    store.delete_user(u.id)
    assert store.user(u.id) is None
    assert store.credentials(u.id) == []
    assert store.credential(b"c") is None
    assert store.session_user(session) is None
    assert store.take_invite(invite) is None
    assert store.conn.execute("select count(*) from credentials").fetchone()[0] == 0
    assert store.conn.execute("select count(*) from sessions").fetchone()[0] == 0
    assert store.conn.execute("select count(*) from invites").fetchone()[0] == 0


def test_the_store_may_be_used_from_another_thread(store: AuthStore) -> None:
    """serve's test client, and uvicorn's thread pool, call the store off the thread that opened it."""
    import threading

    u = store.add_user("Pat", "member")
    seen: list[object] = []
    t = threading.Thread(target=lambda: seen.append(store.user(u.id)))
    t.start()
    t.join()
    assert seen == [u]


def test_peeking_at_an_invite_does_not_consume_it(store: AuthStore, clock: Clock) -> None:
    """Registration options need the invite's user before the passkey exists; a verified passkey takes it."""
    u = store.add_user("Pat", "member")
    token = store.make_invite(u.id, hours=1)
    assert store.peek_invite(token) == u
    assert store.peek_invite(token) == u
    assert store.peek_invite("never-issued") is None
    clock.t += 3601
    assert store.peek_invite(token) is None


def test_ending_one_session_leaves_the_users_other_devices_signed_in(store: AuthStore) -> None:
    u = store.add_user("Pat", "member")
    phone, laptop = store.make_session(u.id, days=30), store.make_session(u.id, days=30)
    store.end_session(phone)
    assert store.session_user(phone) is None
    assert store.session_user(laptop) == u
    store.end_session("never-issued")  # nothing to end is fine
