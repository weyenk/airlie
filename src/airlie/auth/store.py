"""The user store: people, their invites, passkey credentials and sessions, in one SQLite file (WAL) under
observability.state_dir. Invite and session tokens are stored as SHA-256 digests, so a copy of the file signs
nobody in. Nothing in here logs.

A session row records how it was minted (`auth_via`) and, when an IdP minted it, the absolute instant its
expiry may never be pushed past (`ceiling`). `session_renewal` is the only reader of both, so there is one
place that decides how far a cookie may be pushed; the ceiling is written at minting rather than derived from
the current setting — see `session_renewal` for why."""

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

#: What `AuthStore.session_renewal` answers: a session's `expires_at`, and the absolute moment past which its
#: expiry may never be pushed — or None when nothing caps it and the caller's own default applies. A None
#: expiry means there is no such session. The ceiling is an instant rather than a span because the thing it
#: bounds is a row's `expires_at`, which is also an instant; see `session_renewal`.
SessionRenewal = tuple[float | None, float | None]


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


@dataclass(frozen=True)
class PushSubscription:
    """One browser's Web Push subscription: where to POST and the keys the payload is encrypted to."""

    endpoint: str
    user_id: str
    p256dh: str
    auth: str
    created_at: float


@dataclass(frozen=True)
class SourceLink:
    """One person's linked account at a repository source. The tokens are sealed (sources/vault.py)."""

    user_id: str
    source: str
    login: str
    account_id: int
    access_token: bytes
    access_expires_at: float | None
    refresh_token: bytes | None
    refresh_expires_at: float | None
    created_at: float


@dataclass(frozen=True)
class OIDCBinding:
    """One IdP subject, bound to one airlie person by an admin.

    `iss` + `sub` is the identity (the spec's "no auto-link by email"): `email_display` is exactly what an
    admin saw when they made the link, kept so the list is readable, never read as a key. `groups_cached_at`
    is when the groups behind the binding were last seen, for whoever wants to age them out later.
    """

    iss: str
    sub: str
    user_id: str
    email_display: str | None
    groups_cached_at: float | None
    bound_at: float
    bound_by: str


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
create table if not exists push_subscriptions (
    endpoint text primary key, user_id text not null references users(id) on delete cascade,
    p256dh text not null, auth text not null, created_at real not null);
create table if not exists notification_settings (
    user_id text not null references users(id) on delete cascade, kind text not null,
    enabled integer not null, primary key (user_id, kind));
-- no foreign key: the bootstrap admin and the API key pin too, and are no row of users
create table if not exists chat_pins (
    user_id text not null, repo text not null collate nocase, created_at real not null,
    primary key (user_id, repo));
-- no foreign key, as chat_pins: the admin key and the bootstrap admin link too
create table if not exists source_links (
    user_id text not null, source text not null, login text not null, account_id integer not null,
    access_token blob not null, access_expires_at real, refresh_token blob, refresh_expires_at real,
    created_at real not null, primary key (user_id, source));
-- the primary key is the federation pair, not the user: one person may be bound at two issuers, and the same
-- `sub` at two issuers is two people. Cascade because a binding to a person airlie no longer has must not
-- survive to mint them a session (api.py refuses a binding without a user; this is what drops it).
-- `bound_by` has no foreign key on purpose: the admin who clicked Bind is itself either a user row or the
-- API key / bootstrap admin, which are no row of users, as with chat_pins and source_links above.
create table if not exists oidc_bindings (
    iss text not null, sub text not null, user_id text not null references users(id) on delete cascade,
    email_display text, groups_cached_at real, bound_at real not null, bound_by text not null,
    primary key (iss, sub));
"""

#: `sessions` shipped as `(digest, user_id, expires_at)`, and `SCHEMA` is create-table-if-not-exists only, so
#: it cannot evolve a table that is already there. These columns are added on open, guarded by `table_info`,
#: the way `chat/threads.py` and `update/store.py` do it. All of them are nullable, and NULL is a value with
#: meaning rather than a gap: a row that predates them is a passkey session with no ceiling, which is exactly
#: how `session_renewal` reads it (see there). Adding a constraint instead would rewrite the table on
#: someone's deployed box for nothing — the cap has to be absent for those rows, not defaulted.
#:
#: `sid`, `iss` and `sub` are added for the same cost as the first two and are written by nothing yet: the
#: spec has a later pass end what an IdP minted for a subject who left the company, which needs to know which
#: subject minted a session (SCIM itself is out of that pass's scope, and a column no code touches cannot be
#: tested for anything but that it accepts a value). Getting them onto a deployed file is the slow part, so
#: they ride along now; `auth_via` and `ceiling` are the two the renewal path reads on every request.
_SESSION_COLUMNS = ("auth_via", "ceiling", "sid", "iss", "sub")

#: `sessions.auth_via`: the session was minted by the OIDC callback, and is capped by
#: `auth.oidc.session_hours` instead of `auth.session_days`. The only value written today; a passkey login
#: writes nothing.
VIA_OIDC = "oidc"


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
        self._upgrade()

    def _upgrade(self) -> None:
        """Add the columns a shipped `auth.sqlite` predates.

        `SCHEMA` runs through `executescript`, which opens a transaction — and `pragma journal_mode=wal`
        inside that transaction fails on a file that already has data. So the pragmas stay in their own
        `executescript` and this migration is separate: `create table if not exists` in `SCHEMA` still creates
        `oidc_bindings` fresh, and the probe below only handles the file that predates it.

        `table_info` is the probe rather than `pragma user_version`: it reports what this file actually holds,
        which makes the migration idempotent and self-healing (a partially migrated file is finished, not
        skipped) — the same shape `chat/threads.py` and `update/store.py` use.
        """
        have = {row[1] for row in self.conn.execute("pragma table_info(sessions)")}
        for column in _SESSION_COLUMNS:
            if column not in have:
                self.conn.execute(f"alter table sessions add column {column} text")

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
        self.conn.execute("delete from chat_pins where user_id = ?", (id,))
        self.conn.execute("delete from source_links where user_id = ?", (id,))

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
    def make_session(
        self, user_id: str, days: float, *, via: str | None = None, ceiling_hours: float | None = None
    ) -> str:
        """Mint a session, and record the instant past which it may never be renewed.

        `via` says what minted the cookie — `VIA_OIDC` for the callback, nothing for a passkey login — and is
        what `session_renewal` pairs with the ceiling. Minting and renewing are the two places a session's
        lifetime is decided, so both read the same row: a caller cannot mint a capped session that then renews
        like a passkey one by passing a flag to only one of them.

        `ceiling_hours` is measured from the moment of minting, and named in hours because the setting that
        supplies it is `auth.oidc.session_hours`. It is a separate argument rather than a second use of `days`
        because the two are not the same length: an IdP-minted session's expiry and its ceiling both come
        from one configured number of hours, and a caller squeezed into `days: int` would reach for
        `hours // 24` and mint a zero-length session. `days` keeps the type it has always had, so every
        existing call site reads unchanged.

        Supplying a ceiling with no `via` is refused rather than guessed at, and `session_renewal` reads the
        two back together, so the pair cannot drift apart into a row that is capped but unlabelled or the
        reverse.
        """
        if ceiling_hours is not None and via is None:
            raise ValueError("a capped session has to say what minted it, or the cap is not remembered")
        token = secrets.token_urlsafe(32)
        expires_at = self._now() + days * 86400
        self.conn.execute(
            "insert into sessions (digest, user_id, expires_at, auth_via, ceiling) values (?, ?, ?, ?, ?)",
            (
                _digest(token),
                user_id,
                expires_at,
                via,
                None if ceiling_hours is None else self._now() + ceiling_hours * 3600,
            ),
        )
        return token

    def session_renewal(self, token: str) -> SessionRenewal:
        """This session's `(expires_at, ceiling)` — the two numbers that decide whether a request may push its
        expiry out, and the instant it may never push it past.

        The ceiling is None when the session may be renewed the way airlie has always renewed one, to the
        caller's own default (`auth.session_days`). Otherwise it is an absolute instant, and the caller must
        leave the expiry at or before it. That is the spec's rule that a session signed in through the IdP "is
        not silently renewed past" `auth.oidc.session_hours`: the callback minted the row with its ceiling, so
        ordinary use can extend the cookie up to that instant and no further, and the only way past it is to
        sign in again and let the IdP say the person still works here.

        Stored, and deliberately not recomputed from `auth.oidc.session_hours` on each read. The ceiling is
        a *fact about the sign-in* — how long that IdP assertion was good for — not a policy dial: an operator
        who lowers `session_hours` is stating how long the next session should last, and re-deriving from the
        current setting would shorten a cookie somebody is holding by an amount nothing told them about. The
        session ends on its own within one cap period either way.

        Both numbers come from one read, so the rule cannot be split: a caller comparing a separately read
        `session_expires_at` against its own idea of a cap could be handed numbers from two different rows in
        time. `token` is digested like every other token here, so the caller's string never reaches the file.

        A row that predates these columns, or was minted by any other route, has both NULL and reads as an
        ordinary session: `(expires_at, None)` is today's behaviour unchanged.
        """
        row = self.conn.execute(
            "select expires_at, ceiling from sessions where digest = ?", (_digest(token),)
        ).fetchone()
        if row is None:
            return (None, None)
        return (float(row[0]), None if row[1] is None else float(row[1]))

    def session_user(self, token: str) -> User | None:
        row = self.conn.execute(
            "select user_id, expires_at from sessions where digest = ?", (_digest(token),)
        ).fetchone()
        if row is None or row[1] < self._now():
            return None
        return self.user(row[0])

    def session_expires_at(self, token: str) -> float | None:
        row = self.conn.execute(
            "select expires_at from sessions where digest = ?", (_digest(token),)
        ).fetchone()
        return None if row is None else float(row[0])

    def extend_session(self, token: str, days: int) -> None:
        """Push this session's expiry to `days` from now; the token stays the same.

        The cap lives in the caller's numbers, not in a check here: `serve/app.py` reads `session_renewal` and
        passes a `days` that cannot exceed the ceiling. An unbounded `extend_session` stays unbounded because
        that is what it says it does — a caller that skipped the ceiling is visible in the diff rather than
        silently corrected here into a different lifetime than it asked for.
        """
        self.conn.execute(
            "update sessions set expires_at = ? where digest = ?",
            (self._now() + days * 86400, _digest(token)),
        )

    def end_session(self, token: str) -> None:
        """Sign this one device out; the user's other sessions stay."""
        self.conn.execute("delete from sessions where digest = ?", (_digest(token),))

    def end_sessions(self, user_id: str) -> None:
        self.conn.execute("delete from sessions where user_id = ?", (user_id,))

    def prune_sessions(self) -> int:
        """Drop every session that cannot be used any more, and say how many went: rows past their expiry, and
        rows whose person airlie no longer has.

        The second half is what makes a revocation real. A subject cut off at the IdP stops minting new
        sessions at the callback, and `delete_user` cascades in SQL — but a session row can still outlive its
        person when the row was removed straight from the file (an operator's sqlite3), and a table that holds
        a list of people who cannot sign in is a table an operator cannot read.

        Expiry is folded in for the same reason: `session_user` refuses a past-expiry row, so those rows are
        dead weight rather than a risk, and a session store that only ever grows is its own problem
        eventually.

        That is the whole promise. Nothing schedules this — an install that never calls it loses nothing,
        because every refusal above already holds. The count comes from the two statements' own `rowcount`
        rather than a `count(*)` before and after, so the number it prints is the number of rows it deleted
        and not the number of rows that happened to be in the table.
        """
        expired = self.conn.execute("delete from sessions where expires_at < ?", (self._now(),))
        gone = expired.rowcount
        orphans = self.conn.execute("delete from sessions where user_id not in (select id from users)")
        return gone + orphans.rowcount

    # ---- push subscriptions and notification settings ----
    def save_push(self, user_id: str, endpoint: str, p256dh: str, auth: str) -> None:
        self.conn.execute(
            "insert into push_subscriptions (endpoint, user_id, p256dh, auth, created_at)"
            " values (?, ?, ?, ?, ?) on conflict (endpoint) do update"
            " set user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth",
            (endpoint, user_id, p256dh, auth, self._now()),
        )

    def push_subscriptions(self, user_id: str | None = None) -> list[PushSubscription]:
        """One person's subscriptions, or everyone's when `user_id` is None."""
        where, args = ("", ()) if user_id is None else (" where user_id = ?", (user_id,))
        rows = self.conn.execute(
            "select endpoint, user_id, p256dh, auth, created_at from push_subscriptions"
            f"{where} order by created_at, endpoint",
            args,
        ).fetchall()
        return [PushSubscription(*row) for row in rows]

    def drop_push(self, endpoint: str) -> None:
        self.conn.execute("delete from push_subscriptions where endpoint = ?", (endpoint,))

    def notification_kinds(self, user_id: str) -> dict[str, bool]:
        """The kinds this person has set; a kind with no row is left to the configured default."""
        rows = self.conn.execute(
            "select kind, enabled from notification_settings where user_id = ?", (user_id,)
        ).fetchall()
        return {kind: bool(enabled) for kind, enabled in rows}

    def set_notification_kind(self, user_id: str, kind: str, on: bool) -> None:
        self.conn.execute(
            "insert into notification_settings (user_id, kind, enabled) values (?, ?, ?)"
            " on conflict (user_id, kind) do update set enabled = excluded.enabled",
            (user_id, kind, int(on)),
        )

    # ---- the chat panel's pinned repositories ----
    def pins(self, user_id: str) -> list[str]:
        """This person's pinned repositories ("owner/name"), in the order they were pinned."""
        rows = self.conn.execute(
            "select repo from chat_pins where user_id = ? order by created_at, rowid", (user_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def pin(self, user_id: str, repo: str) -> None:
        """Pin `repo`; one already pinned, in any case, keeps its place and its spelling."""
        self.conn.execute(
            "insert into chat_pins (user_id, repo, created_at) values (?, ?, ?) on conflict do nothing",
            (user_id, repo, self._now()),
        )

    def unpin(self, user_id: str, repo: str) -> bool:
        """False when `repo` was not pinned."""
        cur = self.conn.execute("delete from chat_pins where user_id = ? and repo = ?", (user_id, repo))
        return cur.rowcount > 0

    # ---- linked repository-source accounts ----
    def link(self, user_id: str, source: str) -> SourceLink | None:
        row = self.conn.execute(
            "select user_id, source, login, account_id, access_token, access_expires_at, refresh_token,"
            " refresh_expires_at, created_at from source_links where user_id = ? and source = ?",
            (user_id, source),
        ).fetchone()
        return None if row is None else SourceLink(*row)

    def save_link(self, link: SourceLink) -> None:
        self.conn.execute(
            "insert into source_links (user_id, source, login, account_id, access_token, access_expires_at,"
            " refresh_token, refresh_expires_at, created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " on conflict (user_id, source) do update set login = excluded.login,"
            " account_id = excluded.account_id, access_token = excluded.access_token,"
            " access_expires_at = excluded.access_expires_at, refresh_token = excluded.refresh_token,"
            " refresh_expires_at = excluded.refresh_expires_at, created_at = excluded.created_at",
            (
                link.user_id,
                link.source,
                link.login,
                link.account_id,
                link.access_token,
                link.access_expires_at,
                link.refresh_token,
                link.refresh_expires_at,
                link.created_at,
            ),
        )

    def drop_link(self, user_id: str, source: str) -> bool:
        """False when there was no link."""
        cur = self.conn.execute(
            "delete from source_links where user_id = ? and source = ?", (user_id, source)
        )
        return cur.rowcount > 0

    # ---- IdP subjects bound to people (auth.oidc; docs/superpowers/specs/2026-11-26-oidc-sign-in-design.md)
    def lookup_binding(self, iss: str, sub: str) -> str | None:
        """The person bound to this verified subject, or None when nobody is.

        The issuer is part of the key: an install may swap IdPs, and a `sub` is the issuer's identifier rather
        than ours, so the same string at two issuers is two people.

        Returning a `user_id` is not an admission ticket. The callback still loads the row and refuses a
        binding whose person airlie no longer has (and the cascade in `oidc_bindings` normally removes such a
        binding the moment the user is deleted) — the issuer's namespace never occupies airlie's primary key.
        """
        row = self.conn.execute(
            "select user_id from oidc_bindings where iss = ? and sub = ?", (iss, sub)
        ).fetchone()
        return None if row is None else str(row[0])

    def bind_subject(
        self,
        *,
        user_id: str,
        iss: str,
        sub: str,
        bound_by: str,
        email_display: str | None = None,
        groups_cached_at: float | None = None,
    ) -> OIDCBinding:
        """Link one subject to one person, replacing any previous link for that subject.

        Conflict-replace rather than insert-or-fail: re-binding is how an admin corrects a link made to the
        wrong person, and the alternative is a route that 409s at the one moment an operator is fixing a
        mistake. One subject still maps to exactly one person, because the primary key is the pair.

        The caller vouches for `user_id` (the route 404s on an unknown one) and for `iss`/`sub` being read
        from a *verified* ID token — that is what makes this safe to expose to an admin, and why nothing here
        consults an email.

        Raises `sqlite3.IntegrityError` for a `user_id` with no row: the foreign key is the last line keeping
        a binding from pointing at somebody airlie does not have.
        """
        binding = OIDCBinding(iss, sub, user_id, email_display, groups_cached_at, self._now(), bound_by)
        self.conn.execute(
            "insert into oidc_bindings (iss, sub, user_id, email_display, groups_cached_at, bound_at,"
            " bound_by) values (?, ?, ?, ?, ?, ?, ?) on conflict (iss, sub) do update set"
            " user_id = excluded.user_id, email_display = excluded.email_display,"
            " groups_cached_at = excluded.groups_cached_at, bound_at = excluded.bound_at,"
            " bound_by = excluded.bound_by",
            (
                binding.iss,
                binding.sub,
                binding.user_id,
                binding.email_display,
                binding.groups_cached_at,
                binding.bound_at,
                binding.bound_by,
            ),
        )
        return binding

    def unbind_subject(self, iss: str, sub: str) -> bool:
        """Drop a link. False when there was none, so the route can answer 404 rather than claim it worked.

        Existing sessions survive: a person's cookie is not this subject's row any more, and ending them is
        the admin's `DELETE /v1/auth/users/{id}/sessions`, not a side effect of editing a link.
        """
        cur = self.conn.execute("delete from oidc_bindings where iss = ? and sub = ?", (iss, sub))
        return cur.rowcount > 0

    def bindings(self, user_id: str | None = None) -> list[OIDCBinding]:
        """Every link, or one person's. An empty list is a deployment nobody has bound yet, not an error."""
        where, args = ("", ()) if user_id is None else (" where user_id = ?", (user_id,))
        rows = self.conn.execute(
            "select iss, sub, user_id, email_display, groups_cached_at, bound_at, bound_by from oidc_bindings"
            f"{where} order by bound_at, iss, sub",
            args,
        ).fetchall()
        return [OIDCBinding(*row) for row in rows]
