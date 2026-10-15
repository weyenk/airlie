"""A story's state on disk (a stopped story resumes from its last finished stage) and the one-story lock."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from airlie.obs import EventSink
from airlie.story.board import Card
from airlie.story.model import StorySetupError

STAGES = ("new", "intake", "refine", "layers", "build", "publish", "review")


def _end_a_torn_line(events: Path) -> None:
    """End a torn last line (a crash mid-append) so the next event starts its own line."""
    if not events.is_file() or events.stat().st_size == 0:
        return
    with events.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        ends_clean = f.read(1) == b"\n"
    if not ends_clean:
        with events.open("ab") as f:
            f.write(b"\n")


@dataclass
class StoryState:
    """stories/<card key>/state.json. `stage` is the last stage that finished. `built` maps a layer number
    ("01") to its branch, commit, run and subtask rows. `attempt` is a layer that stopped part-way: its branch
    and worktree stay for inspection until a resume rebuilds it. `base` is the commit the story builds on, as
    {"ref": "origin/main", "sha": ...}: resolved once, so a resume reads and builds on the same code."""

    path: Path
    card: dict[str, Any]
    repo: str
    stage: str = "new"
    story: dict[str, Any] | None = None
    layers: list[dict[str, Any]] | None = None
    built: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempt: dict[str, str] | None = None
    base: dict[str, str] | None = None
    prs: list[dict[str, Any]] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    blocked: str | None = None

    @classmethod
    def open(cls, path: Path, card: Card, repo: Path) -> StoryState:
        f = path / "state.json"
        if f.is_file():
            try:
                st = cls(path=path, **json.loads(f.read_text()))
            except (ValueError, TypeError) as e:
                raise StorySetupError(
                    f"{path} is damaged ({type(e).__name__}); delete it to start the story over"
                ) from None
            if st.stage not in STAGES:
                raise StorySetupError(f"{path} is damaged (unknown stage); delete it to start the story over")
            if st.repo != str(repo):
                raise StorySetupError(
                    f"this story was started in {st.repo}; resume it there (--repo {st.repo}) "
                    f"or delete {path} to start over"
                )
            _end_a_torn_line(path / "events.jsonl")
            return st
        path.mkdir(parents=True, exist_ok=True)
        _end_a_torn_line(path / "events.jsonl")  # a deleted state file can leave its event log behind
        st = cls(path=path, card=card.to_dict(), repo=str(repo))
        st.save()
        return st

    def save(self) -> None:
        """The new bytes are synced before the rename and the directory after it, so a crash leaves the old
        state or the new one, never a short file."""
        data = asdict(self)
        del data["path"]
        tmp = self.path / "state.json.tmp"
        with tmp.open("w") as f:
            f.write(json.dumps(data, indent=1, default=str))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path / "state.json")
        with suppress(OSError):  # not every filesystem can fsync a directory
            dir_fd = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def done(self, stage: str) -> bool:
        return STAGES.index(self.stage) >= STAGES.index(stage)

    def finish(self, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"{stage!r} is not a stage; the stages are {', '.join(STAGES)}")
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
            held = True
        except BlockingIOError:
            held = False
        if not held:  # yielded outside the except, so a caller's error is not chained to the lock's
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
