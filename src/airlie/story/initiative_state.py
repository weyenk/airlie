"""An initiative's record on disk (D9 of the specs phase, P4 of the planning line, 2026-10-25):
`initiatives/<key>/state.json` holds the dialogue with the person, product's assumptions, every consultation,
the mockup directions, the epics as written and as priced, and how many revisions each epic has had;
`initiatives/<key>/events.jsonl` is its event log. It sits beside the stories' and the epics' so a person can
see why the epics are what they are and what the specialists said.

The shape follows `EpicState`'s: a state that cannot be read is a StorySetupError naming the file, a save is
synced before the rename. Its stages are the specs phase's own: questions, imagining, reading, delivered, and
blocked (a stop that a person's second approval lifts)."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from airlie.board import Item
from airlie.config import AirlieConfig
from airlie.obs import EventSink
from airlie.story.model import StorySetupError
from airlie.story.state import _damaged, _open_events

INITIATIVE_STAGES = ("questions", "imagining", "reading", "delivered", "blocked")
"""questions: product is asking the person. imagining: product leads the consultations and drafts the epics.
reading: the architect reads each draft as its planner. delivered: the epic cards exist. blocked: a stop;
`blocked` says why and `resume` the stage a second approval returns to."""


def initiatives_dir(cfg: AirlieConfig) -> Path:
    return cfg.observability.state_dir / "initiatives"


@dataclass
class InitiativeState:
    """initiatives/<card key>/state.json. `repos` are the initiative's repositories ("owner/name"), the first
    being the one product reads. `qa` is the dialogue: {"q", "asked_at", "a", "answered_at"} per question.
    `consultations` records each seat asked ({"seat", "question", "answer", "steps", "seconds", "round"});
    `surveys` the architect's notes by repository; `mockups` by epic id; `epics` the drafts (and, once cards
    exist, their keys and estimates); `revisions` how many revisions each epic card has had."""

    path: Path
    card: dict[str, Any]
    repos: list[str]
    stage: str = "questions"
    qa: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    consultations: list[dict[str, Any]] = field(default_factory=list)
    surveys: dict[str, str] = field(default_factory=dict)
    mockups: dict[str, dict[str, Any]] = field(default_factory=dict)
    epics: list[dict[str, Any]] = field(default_factory=list)
    revisions: dict[str, int] = field(default_factory=dict)
    runs: list[str] = field(default_factory=list)
    base: dict[str, str] | None = None
    blocked: str | None = None
    resume: str | None = None
    delivered_at: float | None = None

    @classmethod
    def open(cls, path: Path, item: Item, repos: list[str]) -> InitiativeState:
        """The state at `path`, or a new one for `item`. A state that cannot be read, is damaged, or belongs
        to another initiative is a StorySetupError that names state.json (deleting that file starts the
        phase over and keeps its events); so is an event log that cannot be appended to."""
        f = path / "state.json"
        try:
            text = f.read_text() if f.is_file() else None
        except OSError as e:
            raise StorySetupError(
                f"{f} cannot be read ({e.strerror or type(e).__name__}); fix that, or delete it to start the "
                "initiative over"
            ) from None
        except ValueError as e:
            raise _damaged(f, type(e).__name__) from None
        if text is not None:
            try:
                st = cls(path=path, **json.loads(text))
            except (ValueError, TypeError) as e:
                raise _damaged(f, type(e).__name__) from None
            if st.stage not in INITIATIVE_STAGES:
                raise _damaged(f, "unknown stage")
            if st.card.get("key") != item.key:
                raise _damaged(f, f"it belongs to {st.card.get('key')}, not {item.key}")
            _open_events(path)
            return st
        path.mkdir(parents=True, exist_ok=True)
        _open_events(path)
        st = cls(path=path, card=item.to_dict(), repos=list(repos))
        st.save()
        st.emit("initiative_start", card=item.title, repositories=list(repos))
        return st

    def save(self) -> None:
        """Synced before the rename and the directory after it: a crash leaves the old state or the new
        one."""
        data = asdict(self)
        del data["path"]
        tmp = self.path / "state.json.tmp"
        with tmp.open("w") as f:
            f.write(json.dumps(data, indent=1, default=str))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path / "state.json")
        with suppress(OSError):
            dir_fd = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def finish(self, stage: str) -> None:
        if stage not in INITIATIVE_STAGES:
            raise ValueError(
                f"{stage!r} is not an initiative stage; the stages are {', '.join(INITIATIVE_STAGES)}"
            )
        self.stage = stage
        self.save()
        self.emit("stage", stage=stage)

    def emit(self, kind: str, **fields: Any) -> None:
        EventSink(self.path / "events.jsonl", self.path.name).emit(kind, **fields)

    def pending_question(self) -> dict[str, Any] | None:
        """The last question, when the person has not answered it yet."""
        if self.qa and self.qa[-1].get("a") is None:
            return self.qa[-1]
        return None
