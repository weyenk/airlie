"""The per-repository lesson store: a Lesson is recorded when a package is rejected by the readiness
check, when a layer is re-planned after a red gate, and when the coder ran out of steps or context
(absorbed from AIR-22's branches on 2026-10-24, N5 of the planning-line design; `detail` says why the
layer came back). Lessons are written to disk under the observability state dir, keyed by the repository.
The architect's layer brief reads the most recent ones back with `recent`, so it plans layers small enough
to finish. Lessons are read from disk on every call, never cached in memory: a lesson recorded for
repository R in one process run is still present when a new process opens the same state dir and plans a
story on R."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

ENDINGS = ("out_of_steps", "out_of_context", "gate_red", "package_rejected", "owner_decided")
"""How a layer came back: the coder ran out of steps or context, the gate was red after the build (step end
or not), or the architect's package failed the readiness check every time; or (P2) how the story's meeting
ended: at the turn cap, with each owner's part standing (`detail`: the parts)."""


@dataclass
class Lesson:
    """What a size-blocked layer's lesson records (AC1): what it was trying to do, how big it was, and how
    it ran out. `recorded_at` is the timestamp for recency ordering."""

    title: str
    acceptance: list[str] = field(default_factory=list)
    source_files: int = 0
    test_files: int = 0
    steps: int = 0
    context_tokens: int = 0
    ending: str = "out_of_steps"
    timestamp: float = field(default_factory=time.time)
    detail: str = ""
    """The finding kinds of a rejected package, or the gate's first failing step."""


def _repo_key(repo: Path) -> str:
    """A stable, filesystem-safe encoding of the resolved repository path for a lesson file name."""
    return hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()


class LessonsStore:
    """The lessons recorded for one repository, in a JSON file under `state_dir / 'lessons'`. Each call
    reads from disk, so two stores over the same state dir (two processes) share the lessons."""

    def __init__(self, state_dir: Path, repo: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "lessons" / f"{_repo_key(repo)}.json"

    def _read(self) -> list[Lesson]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        if isinstance(raw, dict):
            raw = [raw]
        return [Lesson(**rec) for rec in raw if isinstance(rec, dict)]

    def _write(self, lessons: list[Lesson]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([lesson.__dict__ for lesson in lessons], indent=2) + "\n")

    def record(self, lesson: Lesson) -> None:
        """Append `lesson` to this repository's lessons and persist them to disk."""
        lessons = self._read()
        lessons.append(lesson)
        self._write(lessons)

    def recent(self, n: int = 5) -> list[Lesson]:
        """The most recent `n` lessons for this repository, newest first, read from disk on every call."""
        return self._read()[-n:][::-1]
