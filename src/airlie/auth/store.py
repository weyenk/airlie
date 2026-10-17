"""The user store: people, their invites, passkey credentials and sessions, in one SQLite file (WAL) under
observability.state_dir. Invite and session tokens are stored as SHA-256 digests, so a copy of the file signs
nobody in. Nothing in here logs."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

Role = Literal["admin", "member"]


@dataclass(frozen=True)
class User:
    id: str
    name: str
    login: str | None
    role: Role
    created_at: float


@dataclass(frozen=True)
class Credential:
    """One passkey. `id` is the authenticator's credential id; `sign_count` guards against a cloned key."""

    id: bytes
    user_id: str
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]
    created_at: float


SCHEMA = """
create table if not exists users (
    id text primary key, name text not null, login text unique, role text not null, created_at real not null);
create table if not exists invites (
    digest text primary key, user_id text not null references users(id) on delete cascade,
    expires_at real not null);
create table if not exists credentials (
    id blob primary key, user_id text not null references users(id) on delete cascade,
    public_key blob not null, sign_count integer not null, transports text not null,
    created_at real not null);
create table if not exists sessions (
    digest text primary key, user_id text not null references users(id) on delete cascade,
    expires_at real not null);
"""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthStore:
    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma foreign_keys=on")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- users ----
    def add_user(self, name: str, role: Role, login: str | None = None) -> User:
        user = User(uuid.uuid4().hex, name, login, role, self._now())
        self.conn.execute(
            "insert into users (id, name, login, role, created_at) values (?, ?, ?, ?, ?)",
            (user.id, user.name, user.login, user.role, user.created_at),
        )
        return user

    def _users(self, where: str, args: tuple[object, ...]) -> list[User]:
        rows = self.conn.execute(
            f"select id, name, login, role, created_at from users {where} order by created_at, rowid", args
        ).fetchall()
        return [User(r[0], r[1], r[2], cast(Role, r[3]), r[4]) for r in rows]

    def user(self, id: str) -> User | None:
        found = self._users("where id = ?", (id,))
        return found[0] if found else None

    def user_by_login(self, login: str) -> User | None:
        found = self._users("where login = ?", (login,))
        return found[0] if found else None

    def users(self) -> list[User]:
        return self._users("", ())

    def delete_user(self, id: str) -> None:
        self.conn.execute("delete from users where id = ?", (id,))

    # ---- invites ----
    def make_invite(self, user_id: str, hours: int) -> str:
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "insert into invites (digest, user_id, expires_at) values (?, ?, ?)",
            (_digest(token), user_id, self._now() + hours * 3600),
        )
        return token

    def peek_invite(self, token: str) -> User | None:
        """The invite's user without consuming it (registration options come before the passkey exists)."""
        row = self.conn.execute(
            "select user_id, expires_at from invites where digest = ?", (_digest(token),)
        ).fetchone()
        if row is None or row[1] < self._now():
            return None
        return self.user(row[0])

    def take_invite(self, token: str) -> User | None:
        row = self.conn.execute(
            "delete from invites where digest = ? returning user_id, expires_at", (_digest(token),)
        ).fetchone()
        if row is None or row[1] < self._now():
            return None
        return self.user(row[0])

    # ---- credentials ----
    def add_credential(self, cred: Credential) -> None:
        self.conn.execute(
            "insert into credentials (id, user_id, public_key, sign_count, transports, created_at) "
            "values (?, ?, ?, ?, ?, ?)",
            (
                cred.id,
                cred.user_id,
                cred.public_key,
                cred.sign_count,
                ",".join(cred.transports),
                cred.created_at,
            ),
        )

    def _credentials(self, where: str, args: tuple[object, ...]) -> list[Credential]:
        rows = self.conn.execute(
            f"select id, user_id, public_key, sign_count, transports, created_at from credentials {where} "
            "order by created_at, rowid",
            args,
        ).fetchall()
        return [
            Credential(r[0], r[1], r[2], r[3], tuple(t for t in r[4].split(",") if t), r[5]) for r in rows
        ]

    def credentials(self, user_id: str) -> list[Credential]:
        return self._credentials("where user_id = ?", (user_id,))

    def credential(self, id: bytes) -> Credential | None:
        found = self._credentials("where id = ?", (id,))
        return found[0] if found else None

    def bump_sign_count(self, id: bytes, n: int) -> None:
        self.conn.execute("update credentials set sign_count = ? where id = ?", (n, id))

    # ---- sessions ----
    def make_session(self, user_id: str, days: int) -> str:
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "insert into sessions (digest, user_id, expires_at) values (?, ?, ?)",
            (_digest(token), user_id, self._now() + days * 86400),
        )
        return token

    def session_user(self, token: str) -> User | None:
        row = self.conn.execute(
            "select user_id, expires_at from sessions where digest = ?", (_digest(token),)
        ).fetchone()
        if row is None or row[1] < self._now():
            return None
        return self.user(row[0])

    def end_session(self, token: str) -> None:
        """Sign this one device out; the user's other sessions stay."""
        self.conn.execute("delete from sessions where digest = ?", (_digest(token),))

    def end_sessions(self, user_id: str) -> None:
        self.conn.execute("delete from sessions where user_id = ?", (user_id,))
