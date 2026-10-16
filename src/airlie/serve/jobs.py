"""Background jobs for `task serve`: a studio plan, a studio run or a story, started over HTTP and followed by
long polls on its event log. Jobs live in this process; their run and story directories outlive it."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Job:
    id: str
    kind: str
    events_path: Path
    task: asyncio.Task[dict[str, Any]]
    started: float = field(default_factory=time.time)

    @property
    def state(self) -> str:
        """running | done | failed | cancelled"""
        if not self.task.done():
            return "running"
        if self.task.cancelled():
            return "cancelled"
        return "failed" if self.task.exception() is not None else "done"


def read_events(path: Path, since: int) -> tuple[list[dict[str, Any]], int]:
    """(events, next): the events in the lines after the first `since`, and the line count a follower asks
    from next. A complete line (one ending in a newline) that is no event is skipped but counted: a story
    resumed after a crash has the fragment the crash left in the middle of its log (state.py closes it), and
    stopping there would freeze every follower. Only a last line without its newline is still being written:
    it is held back, and not counted."""
    if not path.is_file():
        return [], since
    complete = path.read_text().split("\n")[:-1]  # what follows the last newline is not a line yet
    events: list[dict[str, Any]] = []
    for line in complete[since:]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, max(since, len(complete))


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def start(
        self, kind: str, job_id: str, events_path: Path, work: Coroutine[Any, Any, dict[str, Any]]
    ) -> Job:
        old = self._jobs.get(job_id)
        if old is not None and old.state == "running":
            work.close()
            raise ValueError(f"job '{job_id}' is already running")
        job = Job(job_id, kind, events_path, asyncio.create_task(work))
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.task.done():
            return False
        return job.task.cancel()

    async def status(self, job_id: str, *, since: int = 0, wait: float = 0.0) -> dict[str, Any] | None:
        """The job's state, its events after `since`, and the result or error once it ended. With `wait` the
        reply is held (a long poll) until there is a new event, the job ends, or `wait` seconds pass."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        deadline = time.monotonic() + max(0.0, wait)
        events, upto = read_events(job.events_path, since)
        while not events and job.state == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            events, upto = read_events(job.events_path, since)
        out: dict[str, Any] = {
            "job_id": job.id,
            "kind": job.kind,
            "state": job.state,
            "events": events,
            "next": upto,
        }
        if out["state"] == "done":
            out["result"] = job.task.result()
        elif out["state"] == "failed":
            exc = job.task.exception()
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out
