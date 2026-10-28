"""`task serve` ticks the line itself (planning-line P5b, D3): every `line.tick_s` seconds it runs one beat of
`story.line.Line` on its own loop, so a person's move or answer is acted on within a minute while serve is up.
The launchd agent keeps its 15 minutes and runs the same beat in another process; the lane locks make the two
safe together, and a beat that finds a lane held by the other logs `busy` and runs the other lane.

Every job a beat starts is registered in this process's `Jobs` as kind `line` with the id
`line-<lane>-<key>` and its events path, so `/v1/airlie/jobs` and the Workflows screen follow a tick-started
build as they follow one a route started. The beat's blocking helpers (SQLite, files, `gh`) run in worker
threads (`Line`); the ticker is cancelled on shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig, ConfigError
from airlie.serve.jobs import Jobs
from airlie.story import factory
from airlie.story.fixes import line_event
from airlie.story.line import Job, Line, Spawn
from airlie.story.phase import paused
from airlie.story.state import lock_held

log = logging.getLogger(__name__)

LineFor = Callable[[Spawn], Awaitable[str]]
"""One beat, given the function that starts a lane's job (a test passes its own)."""


class LineTicker:
    def __init__(
        self,
        cfg: AirlieConfig,
        jobs: Jobs,
        *,
        commit: str | None,
        tick_s: float | None = None,
        beat: LineFor | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.jobs = jobs
        self.commit = commit
        self.tick_s = float(tick_s if tick_s is not None else cfg.line.tick_s)
        self._beat = beat
        self.now = now
        self.last_tick: float | None = None
        self.last_line: str | None = None
        self._task: asyncio.Task[None] | None = None

    # ---- the beat ----

    def _spawn(self, run: Callable[[Job], Awaitable[str]]) -> Spawn:
        """Starts a lane's job in this process's `Jobs` (kind `line`, id `line-<lane>-<key>`), `run` being how
        it is run (the line's `run_job`, which logs it and says what happened)."""

        def spawn(job: Job) -> None:
            async def work() -> dict[str, Any]:
                return {"line": await run(job)}

            try:
                self.jobs.start("line", job.id, job.events, work())
            except (
                ValueError
            ) as e:  # the same job is still running here: its lane holds the lock, so none starts
                log.warning("line: %s", e)

        return spawn

    async def beat(self) -> str:
        """One beat on this process's loop, jobs registered. The line it logs; a beat that fails is that
        line, never an exception (the ticker goes on)."""
        try:
            if self._beat is not None:  # a test's beat: its jobs run their own work
                return await self._beat(self._spawn(lambda job: job.work()))
            scfg = factory.story_config(self.cfg)
            problems = factory.story_problems(scfg)
            if problems:
                return "not ticking: " + "; ".join(problems)
            state_dir = self.cfg.observability.state_dir.expanduser()
            line = Line(scfg, board=factory.make_board(scfg), state_dir=state_dir, serve_commit=self.commit)
            return await line.beat(self._spawn(line.run_job))
        except asyncio.CancelledError:
            raise
        except ConfigError as e:
            return f"not ticking: {e}"
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
