"""Chat threads kept on the server: each person's conversations, their messages (as the AI SDK UI message
shape: id, role, parts) and the chat's todo list, in one SQLite file (WAL) under `<state_dir>/chat/`. A
separate file from the auth store: threads grow large and are written every turn. A thread is reachable only
by its owner; anyone else gets ThreadNotFound, the same as a thread that is not there."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from airlie.chat.kinds import ChatKind
from airlie.chat.tools import TodoList

TITLE_CHARS = 60

SCHEMA = """
create table if not exists threads (
    id text primary key, owner text not null, kind text not null, title text not null,
    todos text not null, created real not null, updated real not null);
create index if not exists threads_owner on threads (owner, updated);
create table if not exists messages (
    seq integer primary key autoincrement,
    thread_id text not null references threads(id) on delete cascade,
    id text not null, role text not null, parts text not null, created real not null,
    usage text, interrupted integer not null);
create index if not exists messages_thread on messages (thread_id, seq);
"""


class ThreadNotFound(Exception):
    pass


@dataclass(frozen=True)
class StoredMessage:
    id: str
    role: Literal["user", "assistant"]
    parts: list[dict[str, Any]]
    created: float
    usage: dict[str, Any] | None = None
    interrupted: bool = False


@dataclass
class Thread:
    id: str
    owner: str
    kind: ChatKind
    title: str
    created: float
    updated: float
    messages: list[StoredMessage] = field(default_factory=list)
    todos: list[dict[str, Any]] = field(default_factory=list)
    repo: str | None = None
    """A repository chat's "owner/name"; None for a plain chat."""
    base_ref: str | None = None
    base_sha: str | None = None
    """The commit `base_ref` named when the session opened: the base of the session branch."""


@dataclass(frozen=True)
class ThreadSummary:
    id: str
    title: str
    kind: ChatKind
    updated: float


def _title_of(message: StoredMessage) -> str:
    if message.role != "user":
        return ""
    for part in message.parts:
        if part.get("type") == "text" and str(part.get("text") or "").strip():
            return str(part["text"]).strip()[:TITLE_CHARS]
    return ""


def _valid_todos(raw: str) -> list[dict[str, Any]]:
    """The stored todos that pass the todo tool's own checks (a bad stored row is dropped, not raised)."""
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [{"id": i.id, "text": i.text, "status": i.status} for i in TodoList.load(rows).items()]


class ThreadStore:
    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma foreign_keys=on")
        self.conn.executescript(SCHEMA)
        have = {r[1] for r in self.conn.execute("pragma table_info(threads)")}
        for column in ("repo", "base_ref", "base_sha"):  # a store made before repository chats
            if column not in have:
                self.conn.execute(f"alter table threads add column {column} text")
        self._on_delete: list[Callable[[Thread], Awaitable[None]]] = []

    def close(self) -> None:
        self.conn.close()

    def _load(self, thread_id: str, owner: str, kind: ChatKind | None = None) -> Thread:
        row = self.conn.execute(
            "select id, owner, kind, title, todos, created, updated, repo, base_ref, base_sha "
            "from threads where id = ?",
            (thread_id,),
        ).fetchone()
        if row is None or row[1] != owner or (kind is not None and row[2] != kind):
            raise ThreadNotFound(thread_id)
        rows = self.conn.execute(
            "select id, role, parts, created, usage, interrupted from messages where thread_id = ? "
            "order by seq",
            (thread_id,),
        ).fetchall()
        messages = [
            StoredMessage(
                r[0],
                cast(Literal["user", "assistant"], r[1]),
                json.loads(r[2]),
                r[3],
                None if r[4] is None else json.loads(r[4]),
                bool(r[5]),
            )
            for r in rows
        ]
        todos = _valid_todos(row[4])
        return Thread(
            row[0],
            row[1],
            cast(ChatKind, row[2]),
            row[3],
            row[5],
            row[6],
            messages,
            todos,
            row[7],
            row[8],
            row[9],
        )

    def exists(self, thread_id: str) -> bool:
        return self.conn.execute("select 1 from threads where id = ?", (thread_id,)).fetchone() is not None

    def open(
        self,
        thread_id: str,
        owner: str,
        kind: ChatKind,
        *,
        repo: str | None = None,
        base_ref: str | None = None,
    ) -> Thread:
        """The thread, created on its first open; `repo` and `base_ref` (a repository chat's) are kept from
        that first open and ignored after it."""
        if not self.exists(thread_id):
            t = self._now()
            self.conn.execute(
                "insert into threads (id, owner, kind, title, todos, created, updated, repo, base_ref) "
                "values (?, ?, ?, '', '[]', ?, ?, ?, ?)",
                (thread_id, owner, kind, t, t, repo, base_ref),
            )
        return self._load(thread_id, owner, kind)

    def set_base(self, thread_id: str, base_sha: str) -> None:
        self.conn.execute("update threads set base_sha = ? where id = ?", (base_sha, thread_id))

    def repository_threads(self) -> list[Thread]:
        """Every repository chat with its session opened (whoever owns it), without messages: serve's restart
        finds their VMs from these."""
        rows = self.conn.execute(
            "select id, owner from threads where kind = 'repository' and base_sha is not null"
        ).fetchall()
        out: list[Thread] = []
        for thread_id, owner in rows:
            t = self._load(thread_id, owner)
            t.messages = []
            out.append(t)
        return out

    def get(self, thread_id: str, owner: str) -> Thread:
        return self._load(thread_id, owner)

    def append(self, thread_id: str, message: StoredMessage) -> None:
        self.conn.execute("begin immediate")
        try:
            self.conn.execute(
                "insert into messages (thread_id, id, role, parts, created, usage, interrupted) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    message.id,
                    message.role,
                    json.dumps(message.parts),
                    message.created,
                    None if message.usage is None else json.dumps(message.usage),
                    int(message.interrupted),
                ),
            )
            self.conn.execute(
                "update threads set updated = ?, title = case when title = '' then ? else title end "
                "where id = ?",
                (self._now(), _title_of(message), thread_id),
            )
            self.conn.execute("commit")
        except BaseException:
            self.conn.execute("rollback")
            raise

    def set_todos(self, thread_id: str, todos: list[dict[str, Any]]) -> None:
        self.conn.execute(
            "update threads set todos = ?, updated = ? where id = ?",
            (json.dumps(todos), self._now(), thread_id),
        )

    def list(self, owner: str, kind: ChatKind | None = None) -> list[ThreadSummary]:
        where, args = ("owner = ?", (owner,)) if kind is None else ("owner = ? and kind = ?", (owner, kind))
        rows = self.conn.execute(
            f"select id, title, kind, updated from threads where {where} order by updated desc, rowid desc",
            args,
        ).fetchall()
        return [ThreadSummary(r[0], r[1], cast(ChatKind, r[2]), r[3]) for r in rows]

    def on_delete(self, fn: Callable[[Thread], Awaitable[None]]) -> None:
        """`fn(thread)` runs before `remove` deletes a thread: a repository chat's VM goes first, and a
        callback that raises keeps the thread."""
        self._on_delete.append(fn)

    async def remove(self, thread_id: str, owner: str) -> bool:
        """`delete`, after the `on_delete` callbacks have run."""
        try:
            thread = self._load(thread_id, owner)
        except ThreadNotFound:
            return False
        thread.messages = []
        for fn in list(self._on_delete):
            await fn(thread)
        return self.delete(thread_id, owner)

    def delete(self, thread_id: str, owner: str) -> bool:
        cur = self.conn.execute("delete from threads where id = ? and owner = ?", (thread_id, owner))
        return cur.rowcount > 0
