"""Per-run directory: runs/<run_id>/{events.jsonl, trace/, team-<i>/, report.md}."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


@dataclass(frozen=True)
class RunDir:
    path: Path
    run_id: str

    @classmethod
    def create(cls, state_dir: Path, run_id: str | None = None) -> RunDir:
        return cls._make(state_dir.expanduser() / "runs", run_id)

    @classmethod
    def create_for_story(cls, state_dir: Path, story_key: str, run_id: str | None = None) -> RunDir:
        """A run of a story: under state_dir/stories/<key>/runs/, so the step events carry its key."""
        return cls._make(state_dir.expanduser() / "stories" / story_key / "runs", run_id)

    @classmethod
    def _make(cls, runs: Path, run_id: str | None) -> RunDir:
        """The run's directory. A given id is reused as is (a resumed run). A fresh id is minted until its
        directory did not exist: two runs started in the same second can draw the same suffix, and sharing
        a directory makes the second one's worktree add fail with "already exists"."""
        if run_id is not None:
            p = runs / run_id
            (p / "trace").mkdir(parents=True, exist_ok=True)
            return cls(p, run_id)
        runs.mkdir(parents=True, exist_ok=True)
        while True:
            rid = new_run_id()
            p = runs / rid
            try:
                p.mkdir(exist_ok=False)
            except FileExistsError:
                continue
            (p / "trace").mkdir(exist_ok=True)
            return cls(p, rid)

    @property
    def story_key(self) -> str | None:
        """The story this run belongs to (a run under stories/<key>/runs/), or None for a task run."""
        if self.path.parent.name == "runs" and self.path.parents[2].name == "stories":
            return self.path.parents[1].name
        return None

    @property
    def events_path(self) -> Path:
        return self.path / "events.jsonl"

    def write_trace(self, rel: str, payload: dict[str, Any]) -> Path:
        out = self.path / "trace" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1, default=str))
        return out
