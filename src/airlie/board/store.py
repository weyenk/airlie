"""The board in SQLite (stdlib sqlite3, WAL): items, their comments, and the key counter. One connection per
store, shared across threads (serve's workers, the pipeline), with a lock around every write so a key is
allocated and its item inserted in one transaction."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from airlie.board.model import LEVELS, STATUSES, Comment, Item, Level, Status, parent_level

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY, n INTEGER NOT NULL, level TEXT NOT NULL, title TEXT NOT NULL,
    description TEXT NOT NULL, status TEXT NOT NULL, parent TEXT, labels_json TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY, item_key TEXT NOT NULL, author TEXT NOT NULL, text TEXT NOT NULL,
    at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS comments_by_item ON comments(item_key, id);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
INSERT OR IGNORE INTO meta (k, v) VALUES ('next_n', '1');
"""
_FIELDS = "key, level, title, description, status, parent, labels_json, created_at, updated_at, created_by"


def _an(level: str) -> str:
    return f"an {level}" if level[0] in "aeiou" else f"a {level}"


class BoardStoreError(ValueError):
    """A level, parent or status that the board refuses; nothing was stored."""


class BoardStore:
    def __init__(self, path: Path, *, prefix: str = "AL") -> None:
        self.path = path
        self.prefix = prefix
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def _db(self) -> sqlite3.Connection:
        """The connection, opened (and the file and its directory made) on first use: serve builds the store
        at startup, and a state_dir that cannot be written must not stop it, nor a test touch the person's
        board. Reads and writes hold the lock, so the first of them opens it once."""
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- reading -------------------------------------------------------------------------------------------

    def get(self, key: str) -> Item:
        """The item `key`; KeyError when there is none."""
        with self._lock:
            return self._get(key)

    def list(
        self, *, level: Level | None = None, status: Status | None = None, parent: str | None = None
    ) -> list[Item]:
        """Items matching every filter given, oldest first."""
        where, args = [], []
        for column, value in (("level", level), ("status", status), ("parent", parent)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        sql = f"SELECT {_FIELDS} FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY n"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            return [self._item(r) for r in rows]

    def export(self) -> dict[str, Any]:
        return {"prefix": self.prefix, "items": [i.to_dict() for i in self.list()]}

    # ---- writing -------------------------------------------------------------------------------------------

    def create(
        self,
        *,
        level: Level,
        title: str,
        description: str = "",
        parent: str | None = None,
        labels: Sequence[str] = (),
        created_by: str = "noah",
        number: int | None = None,
    ) -> Item:
        """A new item, keyed <prefix>-<n>: the next number, or `number` (an import keeping Jira's), after
        which the counter sits past it. BoardStoreError for a level that is not one, a parent that is not on
        the board or not exactly one level up (none is allowed below initiative), or a number that is taken
        or not positive."""
        if level not in LEVELS:
            raise BoardStoreError(f"level must be one of {', '.join(LEVELS)}, not {level!r}")
        if number is not None and number < 1:
            raise BoardStoreError(f"number must be positive, not {number}")
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._check_parent(level, parent)
                next_n = int(self._db.execute("SELECT v FROM meta WHERE k = 'next_n'").fetchone()["v"])
                n = next_n if number is None else number
                key = f"{self.prefix}-{n}"
                if number is not None and self._db.execute(
                    "SELECT 1 FROM items WHERE key = ?", (key,)
                ).fetchone():
                    raise BoardStoreError(f"{key} is already on the board")
                self._db.execute("UPDATE meta SET v = ? WHERE k = 'next_n'", (str(max(next_n, n + 1)),))
                self._db.execute(
                    f"INSERT INTO items ({_FIELDS}, n) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        level,
                        title,
                        description,
                        "todo",
                        parent,
                        json.dumps(list(labels)),
                        now,
                        now,
                        created_by,
                        n,
                    ),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return self._get(key)

    def update(
        self,
        key: str,
        *,
        title: str | None = None,
        description: str | None = None,
        status: Status | None = None,
        parent: str | None = None,
        labels: Sequence[str] | None = None,
    ) -> Item:
        """The item with the fields given changed; a BoardStoreError (a status that is not one, a parent not
        one level up) changes nothing. Re-parenting keeps the item's level."""
        if status is not None and status not in STATUSES:
            raise BoardStoreError(f"status must be one of {', '.join(STATUSES)}, not {status!r}")
        sets: list[str] = []
        args: list[Any] = []
        for column, value in (("title", title), ("description", description), ("status", status)):
            if value is not None:
                sets.append(f"{column} = ?")
                args.append(value)
        if labels is not None:
            sets.append("labels_json = ?")
            args.append(json.dumps(list(labels)))
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                item = self._get(key)
                if parent is not None:
                    self._check_parent(item.level, parent)
                    sets.append("parent = ?")
                    args.append(parent)
                if sets:
                    sets.append("updated_at = ?")
                    args.append(time.time())
                    self._db.execute(f"UPDATE items SET {', '.join(sets)} WHERE key = ?", (*args, key))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return self._get(key)

    def comment(self, key: str, author: str, text: str, *, at: float | None = None) -> Item:
        """`text` appended to the item's comments as `author`, written now or `at` (an import keeps Jira's
        time); KeyError for an unknown key."""
        now = time.time() if at is None else at
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._get(key)
                self._db.execute(
                    "INSERT INTO comments (item_key, author, text, at) VALUES (?, ?, ?, ?)",
                    (key, author, text, now),
                )
                self._db.execute("UPDATE items SET updated_at = ? WHERE key = ?", (time.time(), key))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return self._get(key)

    # ---- inside the lock -----------------------------------------------------------------------------------

    def _check_parent(self, level: Level, parent: str | None) -> None:
        want = parent_level(level)
        if want is None:
            if parent is not None:
                raise BoardStoreError(f"an initiative has no parent (got {parent})")
            return
        if parent is None:  # an epic under no initiative, a story under no epic: as a Jira project has them
            return
        row = self._db.execute("SELECT level FROM items WHERE key = ?", (parent,)).fetchone()
        if row is None:
            raise BoardStoreError(f"parent {parent} is not on the board")
        if row["level"] != want:
            raise BoardStoreError(f"{_an(level)} belongs under {_an(want)}; {parent} is {_an(row['level'])}")

    def _get(self, key: str) -> Item:
        row = self._db.execute(f"SELECT {_FIELDS} FROM items WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return self._item(row)

    def _item(self, row: sqlite3.Row) -> Item:
        comments = self._db.execute(
            "SELECT author, text, at FROM comments WHERE item_key = ? ORDER BY id", (row["key"],)
        ).fetchall()
        return Item(
            key=row["key"],
            level=cast(Level, row["level"]),
            title=row["title"],
            description=row["description"],
            status=cast(Status, row["status"]),
            parent=row["parent"],
            labels=tuple(json.loads(row["labels_json"])),
            comments=tuple(Comment(c["author"], c["text"], c["at"]) for c in comments),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            created_by=row["created_by"],
        )
