"""The update store's own rules: one sqlite file, and nothing in it is ever lost by a check that failed.

These assertions are about shape and survival, so nothing here waits on the clock.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from airlie.update.feed import ChannelPointer, ImageRef, ReleaseJson
from airlie.update.store import CheckOutcome, UpdateStore

IMAGE = ImageRef(repo="ghcr.io/weyenk/airlie", tag="10.1.0", digest="sha256:aaa")
RELEASE = ReleaseJson(
    version="10.1.0",
    released_at="2026-07-29T00:00:00Z",
    git_sha="a" * 40,
    image=IMAGE,
    notes="Adds the status panel.",
    breaking=False,
    state_migration=False,
    schema_epoch=None,
)
POINTER = ChannelPointer(
    version="10.1.0",
    digest="sha256:aaa",
    released_at="2026-07-29T00:00:00Z",
    release_json="releases/10.1.0.json",
    etag='W/"v1"',
)


def _outcome(**over: object) -> CheckOutcome:
    fields: dict[str, object] = {
        "checked_at": "2026-07-29T12:00:00+00:00",
        "ok": True,
        "reason": None,
        "release": RELEASE,
        "pointer": POINTER,
        "etag": 'W/"v1"',
    }
    fields.update(over)
    return CheckOutcome(**fields)  # type: ignore[arg-type]


def _journal_mode(path: Path) -> str:
    """What the file itself says about its journal mode, read on a connection of our own."""
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        conn.close()


def _rows(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _poison_pointer_snapshot(path: Path) -> None:
    """Make every write to the snapshot fail, so a half-applied pass has somewhere to fail."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TRIGGER poison BEFORE INSERT ON pointer_snapshot "
            "BEGIN SELECT RAISE(ABORT, 'poisoned'); END"
        )
        conn.commit()
    finally:
        conn.close()


def test_a_fresh_store_has_no_state(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "update.sqlite")
    assert store.last_check() is None
    assert store.last_good() is None
    assert store.desired() == (None, None)
    assert store.audit() == []
    assert not (tmp_path / "update.sqlite").exists()  # nothing touched it yet
    store.close()


def test_a_recorded_check_survives_reopening_the_store(tmp_path: Path) -> None:
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    store.record_check(_outcome())
    store.close()

    reopened = UpdateStore(path)
    assert reopened.last_check() == _outcome()
    assert reopened.last_good() == _outcome()
    reopened.close()


def test_a_corrupt_database_is_moved_aside_and_kept_not_truncated(tmp_path: Path) -> None:
    path = tmp_path / "update.sqlite"
    path.write_bytes(b"this is not a database at all")

    store = UpdateStore(path)
    assert store.last_check() is None
    assert store.last_good() is None

    kept = tmp_path / "update.sqlite.corrupt"
    assert kept.read_bytes() == b"this is not a database at all"
    assert not path.exists()  # the unreadable file was renamed, never overwritten

    store.record_check(_outcome())  # the store is usable from here
    store.close()
    assert UpdateStore(path).last_good() == _outcome()


def test_a_failed_check_does_not_erase_the_last_good_release(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "update.sqlite")
    store.record_check(_outcome())
    failed = _outcome(ok=False, reason="unreachable", release=None, pointer=None, etag=None)
    store.record_check(failed)

    assert store.last_check() == failed
    assert store.last_good() == _outcome()
    store.close()


def test_the_desired_choice_survives_reopen_and_being_cleared(tmp_path: Path) -> None:
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    store.set_desired("sha256:zzz", actor="operator")
    assert store.desired() == ("sha256:zzz", "operator")

    store.close()
    reopened = UpdateStore(path)
    assert reopened.desired() == ("sha256:zzz", "operator")

    reopened.clear_desired(actor="operator")
    assert reopened.desired() == (None, None)
    reopened.close()


def test_audit_rows_say_who_changed_what(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "update.sqlite")
    store.set_desired("sha256:aaa", actor="operator")
    store.clear_desired(actor="operator")

    rows = store.audit()
    assert [row["action"] for row in rows] == ["desired_clear", "desired_set"]
    assert all(row["actor"] == "operator" for row in rows)
    assert all(row["before_json"] and row["after_json"] for row in rows)
    assert all(datetime.fromisoformat(str(row["at"])) for row in rows)
    store.close()


def test_audit_rows_come_from_desired_changes_only(tmp_path: Path) -> None:
    """A check pass writes no audit row: the audit is who *changed* something."""
    store = UpdateStore(tmp_path / "update.sqlite")
    store.record_check(_outcome())
    store.record_check(_outcome(ok=False, reason="unreachable", release=None, pointer=None))
    assert store.audit() == []
    store.close()


def test_the_store_uses_wal_and_can_be_opened_by_two_instances(tmp_path: Path) -> None:
    path = tmp_path / "update.sqlite"
    one = UpdateStore(path)
    one.record_check(_outcome())
    two = UpdateStore(path)
    assert two.last_good() == _outcome()
    assert _journal_mode(path) == "wal"
    one.close()
    two.close()


def test_history_is_newest_first_and_capped_at_five_hundred(tmp_path: Path) -> None:
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    for index in range(505):
        store.record_check(_outcome(checked_at=f"2026-07-29T12:00:{index % 60:02d}+00:00"))
    checks = store.history()
    assert len(checks) == 500
    assert checks[0]["checked_at"] >= checks[-1]["checked_at"]
    assert _rows(path, "check_result") == 500
    store.close()


def test_a_failing_write_leaves_the_previous_state_intact(tmp_path: Path) -> None:
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    store.record_check(_outcome())
    _poison_pointer_snapshot(path)

    later = _outcome(
        checked_at="2026-07-29T13:00:00+00:00",
        pointer=ChannelPointer(
            version="10.2.0",
            digest="sha256:bbb",
            released_at="2026-07-30T00:00:00Z",
            release_json="releases/10.2.0.json",
            etag='W/"v2"',
        ),
    )
    with pytest.raises(sqlite3.DatabaseError):
        store.record_check(later)

    assert store.last_good() == _outcome()
    assert store.last_check() == _outcome()
    assert _rows(path, "check_result") == 1
    store.close()
