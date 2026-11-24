"""One sqlite file holding everything the updater knows and everything the operator chose.

Mirrors the board store's connection discipline (board/store.py): one connection shared across
threads behind a lock, write-immediate transactions, journal WAL, a busy timeout so the hub and
a CLI process don't fail on each other.

Two rules this module exists to enforce:

- **A check that failed never erases the last good release.** Every pass lands in
  ``check_result``, but ``pointer_snapshot`` — the last good pair — is only rewritten by a pass
  that ended good. So a 304, a 503 and a truncated file all leave the answer intact, and there is
  no second copy of the truth to drift out of sync with the first.
- **Nothing unreadable is discarded.** A file that won't open is moved to ``*.corrupt`` and left
  there, then a fresh one is created. A store that silently starts empty is indistinguishable from
  one that genuinely has no history, which is the worst possible failure for this feature: the
  panel would say "up to date" when the truth was unrecoverable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from airlie.update.feed import ChannelPointer, ImageRef, ReleaseJson

# Both tables are bounded: 500 passes is months of checking, and an unbounded table in a file the
# hub holds open forever is a slow leak nobody notices until the file is large.
_RETAIN = 500
_BUSY_TIMEOUT_MS = 5_000


class UpdateStoreError(RuntimeError):
    """The ledger could not be opened, or a stored row could not be read back."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS check_result (
    id INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    reason TEXT,
    release_json TEXT,
    pointer_json TEXT,
    etag TEXT
);
CREATE TABLE IF NOT EXISTS pointer_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pointer_json TEXT NOT NULL,
    release_json TEXT,
    etag TEXT
);
CREATE TABLE IF NOT EXISTS desired (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    digest TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT
);
"""


@dataclass(frozen=True)
class CheckOutcome:
    """One check pass, as stored. This is what the panel renders and what rollback reads."""

    checked_at: str
    ok: bool
    reason: str | None = None
    release: ReleaseJson | None = None
    pointer: ChannelPointer | None = None
    etag: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class UpdateStore:
    """The update store. Same WAL / ``check_same_thread=False`` / lock pattern as board/store.py."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _read_db(self) -> sqlite3.Connection | None:
        """A connection for reads, or None when there is nothing readable yet.

        Reads never create the database. The panel has to distinguish "no history" from
        "history we could not read", and a read that opened a fresh ledger on its way
        back would erase that difference.
        """
        if self._conn is None and not self.path.exists():
            return None
        try:
            return self._conn if self._conn is not None else self._open(quarantine=False)
        except sqlite3.DatabaseError:
            self.close()
            self._move_aside_unreadable()
            return None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.row_factory = sqlite3.Row
        return conn

    def _open(self, *, quarantine: bool = True) -> sqlite3.Connection:
        """Open the ledger, quarantining an unreadable file when allowed.

        ``quarantine=False`` is the read path: moving the file aside and creating a new
        one would turn "we lost the history" into "there was never any history".
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = self._connect()
        except sqlite3.DatabaseError:
            if not quarantine:
                raise
            self._move_aside_unreadable()
            conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        except sqlite3.DatabaseError:
            conn.close()
            if not quarantine:
                raise
            self._move_aside_unreadable()
            conn = self._connect()
            conn.executescript(_SCHEMA)
        return conn

    def _move_aside_unreadable(self) -> None:
        """Keep the unreadable file; never overwrite a path that already holds one.

        A corrupt store is the only evidence that the previous one existed, and the panel's
        "no history" answer has to be distinguishable from "we lost the history".
        """
        target = self.path.with_name(f"{self.path.name}.corrupt")
        shift = 2
        while target.exists():
            target = self.path.with_name(f"{self.path.name}.corrupt.{shift}")
            shift += 1
        self.path.replace(target)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except Exception:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def record_check(self, outcome: CheckOutcome) -> None:
        """Record a pass. Always inserts the row; a good pass also upserts the last-known-good pair."""
        with self._lock, self._transaction() as db:
            db.execute(
                "INSERT INTO check_result (checked_at, ok, reason, release_json, pointer_json,"
                " etag) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    outcome.checked_at,
                    int(outcome.ok),
                    outcome.reason,
                    None if outcome.release is None else json.dumps(_release_stored(outcome.release)),
                    None if outcome.pointer is None else json.dumps(_pointer_stored(outcome.pointer)),
                    outcome.etag,
                ),
            )
            if outcome.ok and outcome.pointer is not None:
                db.execute(
                    "INSERT INTO pointer_snapshot (id, pointer_json, release_json, etag)"
                    " VALUES (1, ?, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET pointer_json = excluded.pointer_json,"
                    " release_json = excluded.release_json, etag = excluded.etag",
                    (
                        json.dumps(_pointer_stored(outcome.pointer)),
                        None if outcome.release is None else json.dumps(_release_stored(outcome.release)),
                        outcome.etag,
                    ),
                )
            db.execute(
                "DELETE FROM check_result WHERE id NOT IN"
                " (SELECT id FROM check_result ORDER BY id DESC LIMIT ?)",
                (_RETAIN,),
            )

    def last_check(self) -> CheckOutcome | None:
        with self._lock:
            db = self._read_db()
            if db is None:
                return None
            row = db.execute("SELECT * FROM check_result ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else _outcome_from(row)

    def last_good(self) -> CheckOutcome | None:
        with self._lock:
            db = self._read_db()
            if db is None:
                return None
            row = db.execute("SELECT * FROM check_result WHERE ok = 1 ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else _outcome_from(row)

    def history(self, limit: int = _RETAIN) -> list[dict[str, object]]:
        with self._lock:
            db = self._read_db()
            if db is None:
                return []
            rows = db.execute(
                "SELECT checked_at, ok, reason, release_json FROM check_result"
                " ORDER BY checked_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "checked_at": row["checked_at"],
                "ok": bool(row["ok"]),
                "reason": row["reason"],
                "version": _stored_version(row["release_json"]),
            }
            for row in rows
        ]

    def desired(self) -> tuple[str | None, str | None]:
        with self._lock:
            db = self._read_db()
            if db is None:
                return (None, None)
            row = db.execute("SELECT digest, actor FROM desired WHERE id = 1").fetchone()
        return (None, None) if row is None else (str(row["digest"]), str(row["actor"]))

    def set_desired(self, digest: str, actor: str = "operator") -> None:
        with self._lock, self._transaction() as db:
            before = _current_digest(db)
            db.execute(
                "INSERT INTO desired (id, digest, actor) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET digest = excluded.digest, actor = excluded.actor",
                (digest, actor),
            )
            self._audit(db, "desired_set", actor, before, digest)

    def clear_desired(self, actor: str = "operator") -> None:
        with self._lock, self._transaction() as db:
            before = _current_digest(db)
            db.execute("DELETE FROM desired WHERE id = 1")
            self._audit(db, "desired_clear", actor, before, None)

    def audit(self, limit: int = 50) -> list[dict[str, object]]:
        with self._lock:
            db = self._read_db()
            if db is None:
                return []
            rows = db.execute(
                "SELECT at, action, actor, before_json, after_json FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _audit(
        self,
        db: sqlite3.Connection,
        action: str,
        actor: str,
        before: object,
        after: object,
    ) -> None:
        db.execute(
            "INSERT INTO audit (at, action, actor, before_json, after_json) VALUES (?, ?, ?, ?, ?)",
            (_now(), action, actor, json.dumps(before), json.dumps(after)),
        )
        db.execute(
            "DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT ?)",
            (_RETAIN,),
        )


def _current_digest(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT digest FROM desired WHERE id = 1").fetchone()
    return None if row is None else str(row["digest"])


def _stored_version(release_json: str | None) -> str | None:
    if release_json is None:
        return None
    version: str | None = json.loads(release_json).get("version")
    return version


def _outcome_from(row: sqlite3.Row) -> CheckOutcome:
    release_json = row["release_json"]
    pointer_json = row["pointer_json"]
    return CheckOutcome(
        checked_at=str(row["checked_at"]),
        ok=bool(row["ok"]),
        reason=row["reason"],
        release=None if release_json is None else _release_loaded(json.loads(release_json)),
        pointer=None if pointer_json is None else _pointer_loaded(json.loads(pointer_json)),
        etag=row["etag"],
    )


def _release_stored(release: ReleaseJson) -> dict[str, Any]:
    return {
        "version": release.version,
        "released_at": release.released_at,
        "git_sha": release.git_sha,
        "notes": release.notes,
        "breaking": release.breaking,
        "state_migration": release.state_migration,
        "schema_epoch": release.schema_epoch,
        "image_repo": release.image.repo,
        "image_tag": release.image.tag,
        "image_digest": release.image.digest,
    }


def _release_loaded(stored: dict[str, Any]) -> ReleaseJson:
    return ReleaseJson(
        version=str(stored["version"]),
        released_at=str(stored["released_at"]),
        git_sha=str(stored["git_sha"]),
        image=ImageRef(
            repo=str(stored["image_repo"]),
            tag=str(stored["image_tag"]),
            digest=str(stored["image_digest"]),
        ),
        notes=str(stored["notes"]),
        breaking=bool(stored["breaking"]),
        state_migration=stored["state_migration"],
        schema_epoch=stored["schema_epoch"],
    )


def _pointer_stored(pointer: ChannelPointer) -> dict[str, Any]:
    return {
        "version": pointer.version,
        "digest": pointer.digest,
        "released_at": pointer.released_at,
        "release_json": pointer.release_json,
        "etag": pointer.etag,
    }


def _pointer_loaded(stored: dict[str, Any]) -> ChannelPointer:
    return ChannelPointer(
        version=str(stored["version"]),
        digest=str(stored["digest"]),
        released_at=str(stored["released_at"]),
        release_json=str(stored["release_json"]),
        etag=stored["etag"],
    )
