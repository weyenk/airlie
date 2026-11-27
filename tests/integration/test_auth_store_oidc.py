"""The two things Slice C adds to the store: the link between an IdP subject and a person, and the record of
how a session was minted.

Both are worth testing at the store layer rather than only through the routes, because both are the last place
a wrong answer is cheap to make. A binding that outlives its user would let a deleted person be signed in by
whoever holds the IdP assertion; a session that forgets it came from an IdP gets renewed to 30 days on
ordinary use, which is the drift the whole slice exists to close.

The clock is the shared fake (`tests.support.auth_store.Clock`), so nothing here waits on the real one.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from airlie.auth.store import _SESSION_COLUMNS, VIA_OIDC, AuthStore
from tests.support.auth_store import Clock

ISS = "https://id.acme.com"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    s = AuthStore(tmp_path / "auth.sqlite", now=clock)
    yield s
    s.close()


# ---- the binding table ----


def test_a_bound_subject_resolves_to_its_user_and_nobody_else(store: AuthStore, clock: Clock) -> None:
    dana = store.add_user("Dana", "member", login="dana")
    binding = store.bind_subject(user_id=dana.id, iss=ISS, sub="user-123", bound_by="admin")
    assert store.lookup_binding(ISS, "user-123") == dana.id
    assert (binding.user_id, binding.iss, binding.sub, binding.bound_by) == (
        dana.id,
        ISS,
        "user-123",
        "admin",
    )
    assert binding.bound_at == clock.t
    assert store.lookup_binding(ISS, "user-999") is None
    assert store.lookup_binding("https://other.example", "user-123") is None


def test_the_same_subject_at_two_issuers_is_two_people(store: AuthStore) -> None:
    """`sub` is the issuer's identifier, not ours. An install that swaps IdPs, or one that serves two tenants,
    would otherwise hand whoever holds the string at the new issuer the identity bound at the old one."""
    dana, sam = store.add_user("Dana", "member"), store.add_user("Sam", "member")
    store.bind_subject(user_id=dana.id, iss=ISS, sub="same-sub", bound_by="admin")
    store.bind_subject(user_id=sam.id, iss="https://second.example", sub="same-sub", bound_by="admin")
    assert store.lookup_binding(ISS, "same-sub") == dana.id
    assert store.lookup_binding("https://second.example", "same-sub") == sam.id
    assert {b.iss for b in store.bindings()} == {ISS, "https://second.example"}


def test_rebinding_a_subject_moves_it_to_one_person_and_leaves_one_row(store: AuthStore) -> None:
    """Correcting a link made to the wrong person is the common reason to bind a subject twice. One subject
    must still map to exactly one person afterwards — two rows would make the callback's answer depend on row
    order."""
    dana, sam = store.add_user("Dana", "member"), store.add_user("Sam", "member")
    store.bind_subject(user_id=dana.id, iss=ISS, sub="user-123", bound_by="admin", email_display="d@acme.com")
    again = store.bind_subject(user_id=sam.id, iss=ISS, sub="user-123", bound_by="admin")
    assert store.lookup_binding(ISS, "user-123") == sam.id
    assert len(store.bindings()) == 1
    # the display email is replaced with the link, not merged: stale data on a row an admin just rewrote
    assert again.email_display is None


def test_one_person_may_hold_two_bindings_and_the_list_can_be_filtered_by_person(store: AuthStore) -> None:
    """The list an admin reads is per-subject, and the same person may legitimately be bound at two issuers
    (a tenant swap in progress). Nothing keys a person to one binding, so `bindings(user_id)` has to answer
    with all of theirs."""
    dana = store.add_user("Dana", "member")
    store.bind_subject(user_id=dana.id, iss=ISS, sub="a", bound_by="admin")
    store.bind_subject(user_id=dana.id, iss="https://second.example", sub="a", bound_by="admin")
    store.bind_subject(user_id=store.add_user("Sam", "member").id, iss=ISS, sub="b", bound_by="admin")
    assert len(store.bindings()) == 3
    assert [b.iss for b in store.bindings(dana.id)] == [ISS, "https://second.example"]


def test_deleting_a_user_takes_their_binding_with_it(store: AuthStore) -> None:
    """The cascade is the point: a binding to a person airlie no longer has must not survive to be
    re-adopted by a later row, and the callback's "binding without a user" refusal is the backstop rather
    than the plan."""
    dana = store.add_user("Dana", "member")
    store.bind_subject(user_id=dana.id, iss=ISS, sub="user-123", bound_by="admin")
    store.delete_user(dana.id)
    assert store.lookup_binding(ISS, "user-123") is None
    assert store.bindings() == []
    assert store.conn.execute("select count(*) from oidc_bindings").fetchone() == (0,)


def test_unbind_returns_whether_there_was_anything_to_remove(store: AuthStore) -> None:
    dana = store.add_user("Dana", "member")
    store.bind_subject(user_id=dana.id, iss=ISS, sub="user-123", bound_by="admin")
    assert store.unbind_subject(ISS, "user-123") is True
    assert store.unbind_subject(ISS, "user-123") is False
    assert store.lookup_binding(ISS, "user-123") is None
    assert store.lookup_binding("https://other.example", "user-123") is None  # the wrong issuer never matched


def test_a_binding_names_a_person_airlie_does_not_have_is_refused_by_the_database(store: AuthStore) -> None:
    """The foreign key, not a nicety: it is what keeps a typo'd or invented id from sitting in the table
    pointing at nothing, where the callback would then have to notice."""
    with pytest.raises(sqlite3.IntegrityError):
        store.bind_subject(user_id="no-such-user", iss=ISS, sub="user-123", bound_by="admin")
    assert store.bindings() == []


def test_a_binding_survives_being_written_and_read_back_whole(store: AuthStore, clock: Clock) -> None:
    dana = store.add_user("Dana", "member")
    store.bind_subject(
        user_id=dana.id,
        iss=ISS,
        sub="user-123",
        bound_by="noah",
        email_display="dana@acme.com",
        groups_cached_at=clock.t - 5,
    )
    [got] = store.bindings()
    assert (got.iss, got.sub, got.user_id) == (ISS, "user-123", dana.id)
    assert (got.email_display, got.groups_cached_at, got.bound_at, got.bound_by) == (
        "dana@acme.com",
        clock.t - 5,
        clock.t,
        "noah",
    )


# ---- how a session was minted ----


def test_an_oidc_session_carries_a_ceiling_and_a_passkey_one_does_not(store: AuthStore, clock: Clock) -> None:
    dana = store.add_user("Dana", "member")
    passkey = store.make_session(dana.id, 30)
    oidc = store.make_session(dana.id, 0.5, via=VIA_OIDC, ceiling_hours=12)

    left, ceiling = store.session_renewal(passkey)
    assert left == clock.t + 30 * 86400
    assert ceiling is None  # renewed to the caller's own default, as it has always been

    left, ceiling = store.session_renewal(oidc)
    assert left == clock.t + 0.5 * 86400
    assert ceiling == clock.t + 12 * 3600  # and an ordinary session's 30-day default cannot reach past it
    assert store.session_user(oidc) == dana  # otherwise still a normal session


def test_a_capped_session_must_say_what_minted_it(store: AuthStore) -> None:
    """A ceiling on a session nobody claims is a ceiling nothing will apply: `session_renewal` answers the two
    together, and a row with one and not the other is a bug in the caller, not a state to support."""
    dana = store.add_user("Dana", "member")
    with pytest.raises(ValueError, match="what minted it"):
        store.make_session(dana.id, 0.5, ceiling_hours=1)


def test_an_unknown_token_has_neither_expiry_nor_ceiling(store: AuthStore) -> None:
    assert store.session_renewal("never-issued") == (None, None)


def test_the_ceiling_outlives_the_expiry_it_was_minted_with(store: AuthStore, clock: Clock) -> None:
    """The renewal rule compares `expires_at` against the ceiling on every request. If extending a session
    moved the ceiling with it, the cap would raise itself on the first renewal and there would be no cap at
    all — and nothing but this test would notice.

    So mint one whose expiry and ceiling differ, extend it, and the ceiling must still be the minted one."""
    dana = store.add_user("Dana", "member")
    token = store.make_session(dana.id, 0.25, via=VIA_OIDC, ceiling_hours=1)
    minted_left, minted_ceiling = store.session_renewal(token)
    assert (minted_left, minted_ceiling) == (clock.t + 0.25 * 86400, clock.t + 3600)
    clock.t += 600
    store.extend_session(token, 0.5)
    left, ceiling = store.session_renewal(token)
    assert left == clock.t + 0.5 * 86400
    assert ceiling == minted_ceiling  # unchanged: pushing the expiry cannot push the limit


def test_a_session_row_from_before_the_column_renews_normally(tmp_path: Path, clock: Clock) -> None:
    """A deployed auth.sqlite has sessions without `auth_via`. After the migration those rows must read as
    ordinary sessions — renewed as they always were — and not as capped ones, which would sign everyone out
    early on the strength of a column they were never asked to have."""
    path = tmp_path / "auth.sqlite"
    old = AuthStore(path, now=clock)
    dana = old.add_user("Dana", "member")
    token = old.make_session(dana.id, 30)
    old.conn.execute("update sessions set auth_via = null")  # what the column was before it existed
    old.close()

    again = AuthStore(path, now=clock)
    try:
        left, ceiling = again.session_renewal(token)
        assert left == clock.t + 30 * 86400
        assert ceiling is None
        assert again.session_user(token) == dana
    finally:
        again.close()


def test_a_file_from_before_the_binding_table_is_upgraded_on_open(tmp_path: Path, clock: Clock) -> None:
    """`SCHEMA` is create-table-if-not-exists, so it cannot evolve a file that already exists: the table
    stays missing, and every binding statement would then fail at runtime on an operator's box. The migration
    is what makes an install from yesterday able to bind a subject after the upgrade, with no manual step.

    A freshly created store gets the table from `SCHEMA` itself, so the only way to reach the migration is a
    file written without it — which is what this builds."""
    path = tmp_path / "auth.sqlite"
    shipped = AuthStore(path, now=clock)
    dana = shipped.add_user("Dana", "member")
    session = shipped.make_session(dana.id, 30)
    shipped.conn.execute("drop table oidc_bindings")
    shipped.conn.execute("alter table sessions drop column auth_via")
    shipped.conn.execute("alter table sessions drop column ceiling")
    assert "oidc_bindings" not in _tables(path)
    assert "ceiling" not in _session_columns(path)
    shipped.close()

    upgraded = AuthStore(path, now=clock)
    try:
        assert "oidc_bindings" in _tables(path)
        assert set(_SESSION_COLUMNS) <= set(_session_columns(path))
        upgraded.bind_subject(user_id=dana.id, iss=ISS, sub="user-123", bound_by="admin")
        assert upgraded.lookup_binding(ISS, "user-123") == dana.id
        # the pre-existing row keeps its shape: no ceiling, so it renews as it always did
        assert upgraded.session_renewal(session) == (clock.t + 30 * 86400, None)
    finally:
        upgraded.close()


def test_opening_the_same_file_twice_does_not_migrate_twice(tmp_path: Path, clock: Clock) -> None:
    """serve, the CLI and a test can each open the file. A migration that ran twice would be an `alter table`
    that fails on reboot, which is the worst possible moment to find out."""
    path = tmp_path / "auth.sqlite"
    for _ in range(3):
        s = AuthStore(path, now=clock)
        s.close()
    assert set(_SESSION_COLUMNS) <= set(_session_columns(path))


def test_the_migration_touches_no_other_table(tmp_path: Path, clock: Clock) -> None:
    """Users, credentials, sessions and links must all still be there after the upgrade, with their rows."""
    path = tmp_path / "auth.sqlite"
    first = AuthStore(path, now=clock)
    dana = first.add_user("Dana", "member")
    token = first.make_session(dana.id, 30)
    first.save_push(dana.id, "https://push.example/a", "k", "s")
    first.close()
    second = AuthStore(path, now=clock)
    try:
        assert second.user(dana.id) == dana
        assert second.session_user(token) == dana
        assert [s.endpoint for s in second.push_subscriptions(dana.id)] == ["https://push.example/a"]
    finally:
        second.close()


# ---- which session rows the store will accept at all ----


def test_airlie_cannot_write_a_session_for_a_person_it_does_not_have(store: AuthStore) -> None:
    """The foreign key is what keeps a revoked person from being signed in by a row nobody owns: with
    `pragma foreign_keys=on` (set in `AuthStore.__init__`) no insert through the store can leave a session
    pointing at a missing user, so deleting a person cascades their sessions away and nothing can put a new
    one back. The cascade itself is proven in `test_auth_store.py`'s
    `test_deleting_a_user_cascades_to_credentials_sessions_and_invites`."""
    with pytest.raises(sqlite3.IntegrityError):
        store.make_session(uuid.uuid4().hex, 30)


def test_a_capped_session_that_has_not_expired_is_still_a_signed_in_person(
    store: AuthStore, clock: Clock
) -> None:
    """Nothing revokes a session early: the ceiling bounds how far `session_renewal` may push the expiry, and
    `session_user` judges a row by its expiry alone. A row that has not expired is somebody at a keyboard, so
    any future cleanup would have to leave it alone — and an expired row is refused here at read time rather
    than assumed gone by a sweep that does not exist."""
    dana = store.add_user("Dana", "member")
    oidc = store.make_session(dana.id, 0.5, via=VIA_OIDC, ceiling_hours=12)
    clock.t += 0.25 * 86400  # halfway through its life, and nowhere past the ceiling
    assert store.session_user(oidc) == dana
    assert store.session_expires_at(oidc) == clock.t + 0.25 * 86400
    assert store.session_renewal(oidc)[1] == clock.t + 0.25 * 86400
    clock.t += 0.25 * 86400 + 1  # one second past its own expiry
    assert store.session_user(oidc) is None


def _tables(path: Path) -> set[str]:
    raw = sqlite3.connect(path)
    try:
        return {r[0] for r in raw.execute("select name from sqlite_master where type = 'table'")}
    finally:
        raw.close()


def _session_columns(path: Path) -> list[str]:
    raw = sqlite3.connect(path)
    try:
        return [r[1] for r in raw.execute("pragma table_info(sessions)")]
    finally:
        raw.close()
