"""An epic's record on disk (D9 of the plans phase, P3 of the planning line, 2026-10-24):
`epics/<key>/state.json` holds the plan as the architect wrote it, how many tries it took, the cards made
from it (plan id → board key) and every re-plan; `epics/<key>/events.jsonl` is its event log. It sits beside
the stories' (`stories/<key>/`) so a person can see why the stories are what they are and what came back
from the architect.

The shape follows `StoryState`'s (a state that cannot be read is a StorySetupError naming the file; a save
is synced before the rename), without its stages: an epic is planned once, then its status follows its
stories."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from airlie.board import Item
from airlie.obs import EventSink
from airlie.story.model import StorySetupError
from airlie.story.state import _damaged, _open_events

EPIC_STAGES = ("new", "planned", "building", "review", "done", "blocked")
"""new: not planned yet. planned: the plan is recorded (and its stories made). building / review / done follow
the stories. blocked: the plan was never usable, or a person's epic is unusable; `blocked` says why."""


def epic_needs_work(path: Path) -> bool:
    """Whether the epic whose record is in `path` still needs the pipeline: it has no state yet (or one that
    cannot be read: planning says what is wrong with it), no plan, or a plan some of whose stories have no
    card. Read lightly, from the file, so the board can ask without opening the state."""
    try:
        data = json.loads((path / "state.json").read_text())
        plan, stories = data.get("plan"), data.get("stories") or {}
        return not isinstance(plan, dict) or any(s.get("id") not in stories for s in plan.get("stories", []))
    except (OSError, ValueError, AttributeError, TypeError):
        return True


@dataclass
class EpicState:
    """epics/<card key>/state.json. `repo` is the first repository of the epic ("owner/name"), the one the
    architect reads; `base` the trunk commit it read, as {"ref": "origin/main", "sha": ...}. `plan` is the
    story plan as written (StoryPlan.to_dict), None until one is usable. `attempts` counts the architect's
    tries, `stories` maps a plan id to the board key of its card, `replans` records each re-plan, and
    `seen_comments` is how many of the epic card's comments the plan had been written with."""

    path: Path
    card: dict[str, Any]
    repo: str
    stage: str = "new"
    plan: dict[str, Any] | None = None
    attempts: int = 0
    stories: dict[str, str] = field(default_factory=dict)
    replans: list[dict[str, Any]] = field(default_factory=list)
    blocked: str | None = None
    runs: list[str] = field(default_factory=list)
    base: dict[str, str] | None = None
    seen_comments: int = 0
    estimate: dict[str, Any] | None = None
    """The epic's price as the specs phase wrote it (`costs.Estimate.to_dict`): the first estimate, the one
    revised when the stories were planned, the actual once the epic is done. It mirrors the card's block so
    the ledger (`costs.StoryCosts`) reads it without the board."""

    @classmethod
    def open(cls, path: Path, card: Item, repo: str) -> EpicState:
        """The state at `path`, or a new one for `card`. A state that cannot be read, is damaged, or belongs
        to another epic is a StorySetupError that names state.json (deleting that file starts the epic over
        and keeps its events); so is an event log that cannot be appended to."""
        f = path / "state.json"
        try:
            text = f.read_text() if f.is_file() else None
        except OSError as e:
            raise StorySetupError(
                f"{f} cannot be read ({e.strerror or type(e).__name__}); fix that, or delete it to plan the "
                "epic again"
            ) from None
        except ValueError as e:
            raise _damaged(f, type(e).__name__) from None
        if text is not None:
            try:
                st = cls(path=path, **json.loads(text))
            except (ValueError, TypeError) as e:
                raise _damaged(f, type(e).__name__) from None
            if st.stage not in EPIC_STAGES:
                raise _damaged(f, "unknown stage")
            if st.card.get("key") != card.key:
                raise _damaged(f, f"it belongs to {st.card.get('key')}, not {card.key}")
            _open_events(path)
            return st
        path.mkdir(parents=True, exist_ok=True)
        _open_events(path)
        st = cls(path=path, card=card.to_dict(), repo=repo)
        st.save()
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
        if stage not in EPIC_STAGES:
            raise ValueError(f"{stage!r} is not an epic stage; the stages are {', '.join(EPIC_STAGES)}")
        self.stage = stage
        self.save()
        self.emit("stage", stage=stage)

    def emit(self, kind: str, **fields: Any) -> None:
        EventSink(self.path / "events.jsonl", self.path.name).emit(kind, **fields)
