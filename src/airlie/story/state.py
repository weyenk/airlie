"""A story's state on disk (a stopped story resumes from its last finished stage) and the one-story lock."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from airlie.obs import EventSink
from airlie.story.board import Card
from airlie.story.model import StorySetupError

STAGES = ("new", "intake", "refine", "layers", "build", "publish", "review")


@dataclass
class StoryState:
    """stories/<card key>/state.json. `stage` is the last stage that finished. `built` maps a layer number
    ("01") to its branch, commit, run and subtask rows. `attempt` is a layer that stopped part-way: its branch
    and worktree stay for inspection until a resume rebuilds it."""

    path: Path
    card: dict[str, Any]
    repo: str
    stage: str = "new"
    story: dict[str, Any] | None = None
    layers: list[dict[str, Any]] | None = None
    built: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempt: dict[str, str] | None = None
    prs: list[dict[str, Any]] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    blocked: str | None = None

    @classmethod
    def open(cls, path: Path, card: Card, repo: Path) -> StoryState:
        f = path / "state.json"
        if f.is_file():
            st = cls(path=path, **json.loads(f.read_text()))
            if st.repo != str(repo):
                raise StorySetupError(
                    f"this story was started in {st.repo}; resume it there (--repo {st.repo}) "
                    f"or delete {path} to start over"
                )
            return st
        path.mkdir(parents=True, exist_ok=True)
        st = cls(path=path, card=card.to_dict(), repo=str(repo))
        st.save()
        return st

    def save(self) -> None:
        data = asdict(self)
        del data["path"]
        tmp = self.path / "state.json.tmp"
        tmp.write_text(json.dumps(data, indent=1, default=str))
        tmp.replace(self.path / "state.json")

    def done(self, stage: str) -> bool:
        return STAGES.index(self.stage) >= STAGES.index(stage)

    def finish(self, stage: str) -> None:
        self.stage = stage
        self.blocked = None
        self.save()
        self.emit("stage", stage=stage)

    def emit(self, kind: str, **fields: Any) -> None:
        EventSink(self.path / "events.jsonl", self.path.name).emit(kind, **fields)


@contextmanager
def story_lock(stories_dir: Path) -> Iterator[bool]:
    """True while this caller holds the one-story-at-a-time lock; False when another holder has it (another
    process, or another story in this server). The OS drops the lock if its holder dies."""
    stories_dir.mkdir(parents=True, exist_ok=True)
    with (stories_dir / ".lock").open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
