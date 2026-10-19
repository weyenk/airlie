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


def _ready_to_append(events: Path) -> None:
    """Open the event log for reading and appending, as every event appends to it (it is made when missing),
    and end a torn last line (a crash mid-append) so the next event starts its own line. An OSError when it
    cannot be read or appended to: the first event would raise it, after the story had started."""
    with events.open("a+b") as f:
        if f.seek(0, os.SEEK_END) == 0:
            return
        f.seek(-1, os.SEEK_END)
        if f.read(1) != b"\n":
            f.write(b"\n")


def _open_events(path: Path) -> None:
    """_ready_to_append for the story at `path`, or a StorySetupError that names events.jsonl."""
    events = path / "events.jsonl"
    try:
        _ready_to_append(events)
    except OSError as e:
        why = e.strerror or type(e).__name__
    else:
        return
    raise StorySetupError(  # outside the handler: nothing is chained
        f"{events} cannot be read and appended to ({why}); fix that, or move it away to start a new event log"
    )


def _damaged(f: Path, why: str) -> StorySetupError:
    return StorySetupError(f"{f} is damaged ({why}); delete it to start the story over")


RETIRED = ("mutation_gate", "mutation", "mutation_review")
"""Keys a state saved before 2026-10-19 can hold, for the story's mutation gate, which airlie no longer runs:
mutation testing runs in the repository's CI. They are dropped when the state is read."""


@dataclass
class StoryState:
    """stories/<card key>/state.json. `stage` is the last stage that finished. `built` maps a layer number
    ("01") to its branch, commit, run, subtask rows and the gate it passed (as shown: its PR names it).
    `attempt` is a layer that stopped part-way: its branch and worktree stay for inspection until a resume
    rebuilds it. `base` is the commit the story builds on, as {"ref": "origin/main", "sha": ...}: resolved
    once, so a resume reads and builds on the same code. `gate` is the test gate read from AGENTS.md at that
    commit, as {"commands": [...], "agents_md": <its blob sha>}: read once, so every layer built without a
    person's --test-cmd, resumed or not, passes the same commands."""

    path: Path
    card: dict[str, Any]
    repo: str
    stage: str = "new"
    story: dict[str, Any] | None = None
    survey: str | None = None
    """The architect's notes on the code the request touches, taken while product refined it ("" when none
    were: it failed or is switched off). None: not taken yet."""
    layers: list[dict[str, Any]] | None = None
    built: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempt: dict[str, str] | None = None
    base: dict[str, str] | None = None
    gate: dict[str, Any] | None = None
    prs: list[dict[str, Any]] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)
    blocked: str | None = None
    github_repo: str | None = None

    @classmethod
    def open(cls, path: Path, card: Card, repo: Path, github_repo: str | None = None) -> StoryState:
        """The state at `path`, or a new one. A state that cannot be used (it cannot be read, it is damaged,
        or it belongs to another repository) is a StorySetupError that names state.json: deleting that file,
        not the directory, starts the story over and keeps its events, notes and PR bodies. So is an event log
        that cannot be read and appended to (_open_events): every stage appends to it. Damaged is anything but
        the JSON of a story state, a stage that is none, and a repository, stage or pinned GitHub repository
        that is not text: each is compared or looked up here, and must not fail as anything else.

        `github_repo` is the "owner/name" the story is for: for an issue, the one its repo: label names; for a
        written request, the one the repository's remote names now, as written (None: none on github.com). A
        story keeps the one it was started with (or first opened with) and publishes there only: until it has
        published, a later run that finds another one, or none, is a StorySetupError too."""
        f = path / "state.json"
        try:
            text = f.read_text() if f.is_file() else None
        except OSError as e:  # permissions, a failing disk: a person must look, as at a damaged file
            raise StorySetupError(
                f"{f} cannot be read ({e.strerror or type(e).__name__}); fix that, or delete it to start "
                "the story over"
            ) from None
        except ValueError as e:  # bytes that are not text (UnicodeDecodeError)
            raise _damaged(f, type(e).__name__) from None
        if text is not None:
            try:
                data = json.loads(text)
                if isinstance(data, dict):  # a state from before RETIRED keeps working
                    for key in RETIRED:
                        data.pop(key, None)
                st = cls(path=path, **data)
            except (ValueError, TypeError) as e:
                raise _damaged(f, type(e).__name__) from None
            for name in ("repo", "stage", "github_repo"):
                value = getattr(st, name)
                if not isinstance(value, str) and (name != "github_repo" or value is not None):
                    raise _damaged(f, f"its {name} is not text")
            if st.stage not in STAGES:
                raise _damaged(f, "unknown stage")
            if st.repo != str(repo):
                raise StorySetupError(
                    f"this story was started in {st.repo}; resume it there (--repo {st.repo}) "
                    f"or delete {f} to start over"
                )
            st._hold(github_repo)
            _open_events(path)
            return st
        path.mkdir(parents=True, exist_ok=True)
        _open_events(path)  # a deleted state file can leave its event log behind
        st = cls(path=path, card=card.to_dict(), repo=str(repo), github_repo=github_repo)
        st.save()
        return st

    def _hold(self, github_repo: str | None) -> None:
        """Keep the repository this story publishes to: the first it is opened with, then only that one, until
        it has published (its PRs are open: nothing is published again, wherever the remote points now)."""
        f = self.path / "state.json"
        if self.done("publish"):
            return
        if self.github_repo is None:
            if github_repo is None:
                return
            self.github_repo = github_repo
            try:
                self.save()
            except OSError as e:
                why = e.strerror or type(e).__name__
            else:
                return
            raise StorySetupError(f"{f} cannot be written ({why}); fix that, or delete it to start over")
        if github_repo is None or github_repo.lower() != self.github_repo.lower():
            raise StorySetupError(
                f"{f}: this story publishes to {self.github_repo}, but the remote now names "
                f"{github_repo or 'no github.com repository'}; point the remote back at {self.github_repo}, "
                f"or delete {f} to start the story over"
            )

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
