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


def read_events(path: Path, since: int) -> list[dict[str, Any]]:
    """Event lines after the first `since`; stops at a line still being written."""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines()[since:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            break
    return out


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
        events = read_events(job.events_path, since)
        while not events and job.state == "running" and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            events = read_events(job.events_path, since)
        out: dict[str, Any] = {
            "job_id": job.id,
            "kind": job.kind,
            "state": job.state,
            "events": events,
            "next": since + len(events),
        }
        if out["state"] == "done":
            out["result"] = job.task.result()
        elif out["state"] == "failed":
            exc = job.task.exception()
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out
