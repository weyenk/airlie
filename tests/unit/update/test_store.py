"""The update store's own rules: one sqlite file, and nothing in it is ever lost by a check that failed.

These assertions are about shape and survival, so nothing here waits on the clock.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from airlie.update.feed import ChannelPointer, ImageRef, ReleaseJson
from airlie.update.store import _RETAIN, CheckOutcome, UpdateStore

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


def _replace_image(release: ReleaseJson, image: ImageRef) -> ReleaseJson:
    """One release with a different image block, for tests about the two digests it carries."""
    return replace(release, image=image)


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


def _snapshot_release_json(path: Path) -> str | None:
    """The snapshot table's stored release, read straight from the file.

    Nothing in ``src`` SELECTs ``pointer_snapshot`` — it is the fallback a reader would be added
    to use later — so the only way to assert on it is to read the table the way sqlite would.
    """
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT release_json FROM pointer_snapshot WHERE id = 1").fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


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
    """The write path is what quarantines. A read must never rename the file (the two tests below
    hold that line), so this rule is reached the way a hub reaches it: by writing."""
    path = tmp_path / "update.sqlite"
    path.write_bytes(b"this is not a database at all")

    store = UpdateStore(path)
    store.record_check(_outcome())  # the first write moves the unreadable file aside and starts over

    kept = tmp_path / "update.sqlite.corrupt"
    assert kept.read_bytes() == b"this is not a database at all"  # kept, never truncated
    assert store.last_good() == _outcome()
    store.close()
    assert UpdateStore(path).last_good() == _outcome()


def test_a_read_reports_a_corrupt_database_instead_of_renaming_it(tmp_path: Path) -> None:
    """A read that cannot make sense of the file says so, and leaves the evidence where it is.

    Answering ``None`` here would be indistinguishable from a ledger that never held anything,
    which is the failure this module exists to prevent; renaming it would be a GET destroying the
    history it was asked to report. The next write is what quarantines.
    """
    path = tmp_path / "update.sqlite"
    path.write_bytes(b"this is not a database at all")

    store = UpdateStore(path)
    with pytest.raises(sqlite3.DatabaseError):
        store.last_check()
    with pytest.raises(sqlite3.DatabaseError):
        store.last_good()

    assert path.read_bytes() == b"this is not a database at all"
    assert not (tmp_path / "update.sqlite.corrupt").exists()

    store.record_check(_outcome())  # the write path still quarantines, from the same file
    assert (tmp_path / "update.sqlite.corrupt").read_bytes() == b"this is not a database at all"
    store.close()


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="chmod 000 denies no read on Windows or to root"
)
def test_a_transient_open_failure_on_a_read_never_renames_the_live_ledger(tmp_path: Path) -> None:
    """The two-connection probe: an unreadable-but-perfectly-good file must not be quarantined.

    ``chmod 000`` stands in for the EACCES or EMFILE blip a busy hub actually hits. The file on
    disk is intact, so renaming it is the bug being fixed here: fresh readers would have answered
    "no history" while the writer carried on with the inode the read had just unlinked.
    """
    path = tmp_path / "update.sqlite"
    writer = UpdateStore(path)
    writer.record_check(_outcome())
    assert UpdateStore(path).last_good() == _outcome()  # readable while the file is readable

    path.chmod(0o000)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            UpdateStore(path).last_check()
    finally:
        path.chmod(0o644)

    assert path.exists()  # the live ledger is still the live ledger
    assert not (tmp_path / "update.sqlite.corrupt").exists()
    reader = UpdateStore(path)
    assert reader.last_good() == _outcome()  # and a fresh reader still finds the history
    writer.close()
    reader.close()


@pytest.mark.parametrize(
    "failure",
    [sqlite3.OperationalError("database is locked"), sqlite3.DatabaseError("file is not a database")],
    ids=["locked", "corrupt"],
)
def test_no_sqlite_failure_on_a_read_touches_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """The same guarantee without leaning on what the OS does to a chmod'ed file.

    Both classes matter because they are what a live hub raises — the operational class for a
    lock or a permission blip, plain ``DatabaseError`` for a file whose contents really are
    broken. Neither earns the right to rename a ledger a read was only asked about.
    """
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    store.record_check(_outcome())

    reader = UpdateStore(path)

    def refuse() -> sqlite3.Connection:
        raise failure

    monkeypatch.setattr(reader, "_connect", refuse)
    with pytest.raises(sqlite3.DatabaseError):
        reader.last_check()

    assert path.exists()
    assert not (tmp_path / "update.sqlite.corrupt").exists()
    assert UpdateStore(path).last_good() == _outcome()  # a fresh reader still gets the history
    store.close()


def test_a_good_pass_with_no_release_does_not_replace_the_last_good_release(tmp_path: Path) -> None:
    """A good pass with a pointer and no release document is a hole, not an answer.

    ``last_good`` is what the panel renders, so such a row must not become it: the release the
    feed last published keeps answering until a pass actually brings one.
    """
    store = UpdateStore(tmp_path / "update.sqlite")
    store.record_check(_outcome())
    hole = _outcome(checked_at="2026-07-29T13:00:00+00:00", release=None, etag='W/"v2"')
    store.record_check(hole)

    assert store.last_check() == hole  # the newest pass is still the newest pass
    assert store.last_good() == _outcome()
    store.close()


def test_a_release_less_good_pass_leaves_the_snapshot_pair_intact(tmp_path: Path) -> None:
    """The snapshot is the pair that outlives the row budget, so a pass with nothing to show
    carries nothing into it: blanking ``release_json`` there would poison the fallback."""
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    store.record_check(_outcome())
    assert _snapshot_release_json(path) is not None

    store.record_check(
        _outcome(checked_at="2026-07-29T13:00:00+00:00", release=None, pointer=POINTER, etag='W/"v2"')
    )

    stored = _snapshot_release_json(path)
    assert stored is not None and json.loads(stored)["version"] == "10.1.0"
    store.close()


def test_the_only_good_row_survives_a_prune_at_the_budget(tmp_path: Path) -> None:
    """The prune boundary, swept. An outage longer than the row budget used to delete the only
    release the ledger ever held, which reaches the panel as "this deployment never had a
    release" — the answer the spec forbids. The newest good row sits outside the budget, so the
    table holds at most one row more than it and never forgets what it knows.
    """
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    good = _outcome()
    store.record_check(good)
    failure = _outcome(ok=False, reason="unreachable", release=None, pointer=None, etag=None)

    for _ in range(_RETAIN - 1):  # the budget is exactly full and the good row is inside it
        store.record_check(failure)
    assert _rows(path, "check_result") == _RETAIN
    assert store.last_good() == good

    store.record_check(failure)  # one pass more than the budget, so the good row is out of it
    assert store.last_good() == good
    assert _rows(path, "check_result") == _RETAIN + 1
    assert len(store.history()) == _RETAIN
    store.close()


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


#: ``check_result`` exactly as it stood before a pass carried a ``detail``.
_PRE_DETAIL_DDL = """
CREATE TABLE check_result (
    id INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    reason TEXT,
    release_json TEXT,
    pointer_json TEXT,
    etag TEXT
);
"""


def test_a_ledger_written_before_the_detail_column_still_opens(tmp_path: Path) -> None:
    """``CREATE TABLE IF NOT EXISTS`` never evolves a table that is already there, so every deployed
    ledger predates ``detail``. Without the adoption in ``_open`` the first write after an upgrade
    dies on a missing column — an upgrade that bricks the panel on precisely the installs holding
    history, which is the half of the fleet this feature cannot afford to break."""
    path = tmp_path / "update.sqlite"
    seeded = sqlite3.connect(path)
    seeded.executescript(_PRE_DETAIL_DDL)
    seeded.execute(
        "INSERT INTO check_result (checked_at, ok, reason)"
        " VALUES ('2026-07-28T12:00:00+00:00', 0, 'unreachable')"
    )
    seeded.commit()
    seeded.close()

    store = UpdateStore(path)
    store.record_check(_outcome(detail="channels/stable.json is not JSON: no"))

    assert store.last_check().detail == "channels/stable.json is not JSON: no"
    assert _rows(path, "check_result") == 2  # the old pass is kept, reading back with no detail
    store.close()


def test_a_failure_detail_survives_reopening_the_store(tmp_path: Path) -> None:
    """The reason is the taxonomy the panel renders, so the detail rides beside it instead of being
    folded into it: an operator has to see which file the mirror failed on without the panel losing
    the machine-readable reason its rows key on."""
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    store.record_check(
        _outcome(
            ok=False,
            reason="unsupported_shape",
            detail="/mirrors/a/channels/stable.json is not a file",
        )
    )
    store.close()

    reopened = UpdateStore(path)
    row = reopened.last_check()
    assert row is not None
    assert row.reason == "unsupported_shape"
    assert row.detail == "/mirrors/a/channels/stable.json is not a file"
    reopened.close()


def test_the_pull_digest_survives_reopening_the_store(tmp_path: Path) -> None:
    """The pin the panel recommends has to outlive the check that read it: ``put_desired`` validates
    against the *stored* last-good release, so a pull digest that did not round-trip would be refused
    by the same panel that just offered it."""
    pull = ImageRef(repo=IMAGE.repo, tag=IMAGE.tag, digest=IMAGE.digest, pull_digest="sha256:" + "d" * 64)
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    store.record_check(_outcome(release=_replace_image(RELEASE, pull)))
    store.close()

    reopened = UpdateStore(path)
    good = reopened.last_good()
    assert good is not None and good.release is not None
    assert good.release.image.digest == IMAGE.digest
    assert good.release.image.pull_digest == pull.pull_digest
    # and the snapshot pair carries it too, which is the row that outlives the prune budget
    snapshot = json.loads(_snapshot_release_json(path) or "{}")
    assert snapshot["image_pull_digest"] == pull.pull_digest
    reopened.close()


def test_a_ledger_row_written_before_the_pull_digest_still_loads(tmp_path: Path) -> None:
    """Every deployed ledger holds releases published before the second digest existed. Their stored
    JSON has no such key, and a loader that indexed it would raise on the read the panel is built on —
    turning "I have history" into "there is no last-good release", which reads `unknown`."""
    path = tmp_path / "update.sqlite"
    store = UpdateStore(path)
    store.record_check(_outcome())
    store.close()

    db = sqlite3.connect(path)
    stored = json.loads(db.execute("SELECT release_json FROM check_result WHERE id = 1").fetchone()[0])
    del stored["image_pull_digest"]
    db.execute("UPDATE check_result SET release_json = ?", (json.dumps(stored),))
    db.commit()
    db.close()

    reopened = UpdateStore(path)
    good = reopened.last_good()
    assert good is not None and good.release is not None
    assert good.release.image.digest == IMAGE.digest
    assert good.release.image.pull_digest is None
    reopened.close()
