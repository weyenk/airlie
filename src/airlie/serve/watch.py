"""Noticing writes the server did not make itself: the launchd story tick, `task run`,
`task board:import-jira` and `task auth:invite` write the files `airlie serve` reads, and the page must
learn of them as it learns of its own. A `Watcher` polls `observability.state_dir` and publishes a topic
hint to the live feed for each thing that changed (the page refetches through the existing GET routes, so a
hint carries no data):

- `board` / `users`: `PRAGMA data_version` of `board.sqlite` / `auth.sqlite`, read on a connection the
  watcher keeps open. SQLite changes that number when any *other* connection commits, so one number covers
  every writer, this server's own board and auth stores included, and two commits between polls are one
  hint. The connection is read-only: the watcher never writes to a database, and never creates one.
- `stories` / `<key>`: the size of `stories/<key>/events.jsonl` and the size and mtime of `.../state.json`.
- `runs` / `<run id>`: the size of `runs/<id>/events.jsonl`, for the newest `recent_runs` run directories
  (run ids sort by time, as `airlie.serve.listing.recent_runs` sorts them).

The first poll only records where things stand. A database that does not exist yet is skipped; when it
appears after that, it is a change. Removals are not reported (nothing airlie runs deletes a story or a
run), and a run sliding out of the newest `recent_runs` is not a change."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import NamedTuple

from airlie.serve.feed import Feed

log = logging.getLogger(__name__)

INTERVAL_S = 1.0
"""How often the loop polls while a page is connected."""

Hint = tuple[str, str | None]
"""A topic and, when one thing in it changed, its key: what `Feed.publish` takes."""

# topic to file, in the order hints are returned
_DATABASES = {"board": "board.sqlite", "users": "auth.sqlite"}


class _Seen(NamedTuple):
    versions: dict[str, int | None]  # topic: data_version; None when there is no database to read
    stories: dict[str, tuple[int | None, tuple[int, int] | None]]  # key: (log size, (state mtime, size))
    runs: dict[str, int | None]  # run id: event log size


class _Database:
    """One SQLite file's `PRAGMA data_version`, on a connection kept open between polls (a fresh connection
    has nothing to compare against). Opened read-only, and only once the file exists: connecting would
    create it."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def version(self) -> int | None:
        """None while there is nothing to read: no file (a removed one is let go of, so a new file is read
        afresh), or an error this time (the next poll tries again)."""
        if not self._path.exists():
            self.close()
            return None
        try:
            if self._conn is None:
                uri = f"{self._path.resolve().as_uri()}?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=0.2)
            return int(self._conn.execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.Error:
            self.close()
            return None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _dirs(path: Path) -> list[str]:
    try:
        with os.scandir(path) as entries:
            return [e.name for e in entries if e.is_dir()]
    except OSError:  # not there (yet), or unreadable
        return []


def _stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


class Watcher:
    """`poll()` once, or let `ensure_running()` poll while pages are connected."""

    def __init__(self, state_dir: Path, feed: Feed, *, recent_runs: int = 100) -> None:
        self._state = state_dir
        self._feed = feed
        self._recent = recent_runs
        self._dbs = {topic: _Database(state_dir / name) for topic, name in _DATABASES.items()}
        self._seen: _Seen | None = None  # None: no poll yet, so the next one only records
        self._lock = threading.Lock()  # a poll may come from any thread
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    def poll(self) -> list[Hint]:
        """Publish to the feed, and return, what changed since the last poll (nothing for the first). Hints
        come in a fixed order: board, users, stories by key, runs by id, each once."""
        with self._lock:
            seen = self._look()
            before, self._seen = self._seen, seen
            if before is None:
                return []
            hints = _changes(before, seen)
            for topic, key in hints:
                self._feed.publish(topic, key)
            return hints

    def ensure_running(self, interval_s: float = INTERVAL_S) -> None:
        """When no loop is running: poll at once, which records the starting point (so a write after this
        returns is seen, and the stream calls it before it tells a page it is connected), then start one
        loop that polls every `interval_s` while `feed.subscribers > 0` and ends when it reaches 0. A later
        connection starts a new loop with a fresh baseline. Call it on the event loop."""
        if self.running:
            return
        loop = asyncio.get_running_loop()
        self.poll()
        self._task = loop.create_task(self._run(interval_s))

    def stop(self) -> None:
        """End the loop, let go of the databases and forget where things stood."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        self._forget()

    async def _run(self, interval_s: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                if self._feed.subscribers == 0:
                    return  # no await between this and `finally`: ensure_running cannot see a dying loop
                try:
                    self.poll()
                except Exception:
                    log.exception("watching %s failed; trying again in %ss", self._state, interval_s)
        finally:
            if self._task is asyncio.current_task():  # not already replaced by stop() and a new loop
                self._task = None
                self._forget()

    def _forget(self) -> None:
        with self._lock:
            self._seen = None
            for db in self._dbs.values():
                db.close()

    def _look(self) -> _Seen:
        versions = {topic: db.version() for topic, db in self._dbs.items()}
        stories: dict[str, tuple[int | None, tuple[int, int] | None]] = {}
        root = self._state / "stories"
        for key in sorted(_dirs(root)):
            events = _stat(root / key / "events.jsonl")
            state = _stat(root / key / "state.json")
            stories[key] = (
                None if events is None else events.st_size,
                None if state is None else (state.st_mtime_ns, state.st_size),
            )
        runs: dict[str, int | None] = {}
        runs_dir = self._state / "runs"
        for run_id in sorted(_dirs(runs_dir), reverse=True)[: self._recent]:
            events = _stat(runs_dir / run_id / "events.jsonl")
            runs[run_id] = None if events is None else events.st_size
        return _Seen(versions, stories, runs)


def _changes(before: _Seen, now: _Seen) -> list[Hint]:
    hints: list[Hint] = []
    for topic, version in now.versions.items():
        if version is not None and version != before.versions.get(topic):
            hints.append((topic, None))
    hints += [("stories", key) for key, sig in now.stories.items() if sig != before.stories.get(key)]
    hints += [
        ("runs", run_id)
        for run_id, size in now.runs.items()
        if run_id not in before.runs or size != before.runs[run_id]
    ]
    return hints
