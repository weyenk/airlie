"""`task serve`'s heartbeat. The hand-written story line is retired: nothing here runs a story, and the
board's runs are started by the run dispatcher (`airlie.serve.dispatcher`) the moment a card is approved. What
is left is the beat `/health` and the board's line row read: every `line.tick_s` seconds `LineTicker` records
the time and a line (`dispatcher`, unless a test injects its own beat), and a line that differs from the last
one is a `tick` event on the line's log. The default beat also pumps the dispatcher, so an Approved card that
waited on a workdir lock another process has since freed starts.

Jobs this process holds as kind `line` (id `line-<lane>-<key>`) still show in `lanes()`; with the line retired
none is started, and a lane another process holds shows as `?`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig
from airlie.serve.jobs import Jobs
from airlie.story import jobs as factory
from airlie.story.fixes import line_event
from airlie.story.phase import paused
from airlie.story.state import lock_held

log = logging.getLogger(__name__)

QUIET = "dispatcher"
"""The line a beat reports when it has nothing of its own: board runs are the dispatcher's."""

LineFor = Callable[..., Awaitable[str]]
"""One beat hook for tests."""


class LineTicker:
    def __init__(
        self,
        cfg: AirlieConfig,
        jobs: Jobs,
        *,
        commit: str | None,
        tick_s: float | None = None,
        beat: LineFor | None = None,
        pump: Callable[[], Awaitable[None]] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.jobs = jobs
        self.commit = commit
        self.tick_s = float(tick_s if tick_s is not None else cfg.line.tick_s)
        self._beat = beat
        self._pump = pump
        self.now = now
        self.last_tick: float | None = None
        self.last_line: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def beat(self) -> str:
        """One beat on this process's loop. It runs no story (the line is retired); with a `pump` it asks the
        dispatcher to retry the Approved cards that wait on a workdir lock another process has since freed.
        Tests may inject `_beat`."""
        try:
            if self._beat is not None:
                return await self._beat()
            if self._pump is not None:
                await self._pump()
            return QUIET
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - one bad beat must not end the ticker
            log.exception("line beat failed")
            return f"beat failed ({type(e).__name__}: {e})"

    async def tick(self) -> str:
        """A beat, recorded: its time and line are what `/health` shows, and a line that differs from the last
        one is a `tick` event on the line's log (a quiet minute is not one)."""
        line = await self.beat()
        previous = self.last_line
        self.last_tick, self.last_line = self.now(), line
        if line != previous:
            line_event(self.cfg.observability.state_dir.expanduser(), "tick", line=line)
        return line

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)  # the first beat comes one interval after serve starts
            await self.tick()

    def start(self) -> None:
        if self._task is None and self.cfg.line.serve_ticks:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---- what /health says ----

    def lanes(self) -> dict[str, str | None]:
        """The key each lane is working on: from this process's jobs; `?` when another process holds the lane
        (the launchd tick, `task story`)."""
        stories = factory.stories_dir(self.cfg)
        out: dict[str, str | None] = {}
        for lane in ("build", "plan"):
            running = [j for j in self.jobs.list() if j["kind"] == "line" and j["state"] == "running"]
            mine = next(
                (j["id"].split("-", 2)[2] for j in running if j["id"].startswith(f"line-{lane}-")), None
            )
            out[lane] = mine or ("?" if self._held(stories, lane) else None)
        return out

    @staticmethod
    def _held(stories: Path, lane: str) -> bool:
        return lock_held(stories, lane)  # type: ignore[arg-type]

    def row(self) -> dict[str, Any]:
        return {
            "last_tick": self.last_tick,
            "line": self.last_line,
            "paused": paused(self.cfg.observability.state_dir.expanduser()),
            "lanes": self.lanes(),
        }
