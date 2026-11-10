"""Shared helpers extracted from tests/unit/test_story_tick_epics.py for tests.support.story_tick_epics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from airlie.board import BoardStore
from airlie.engine import FakeEngine
from airlie.story import factory
from airlie.story.publish import Publisher
from tests.support.git import git
from tests.support.story import STORY_ROLES, RecordedGh, checkout_of
from tests.support.story_epic import fenced as block
from tests.support.story_epic_pipeline import EPIC_TEXT, SPEC
from tests.support.story_factory import cfg_for, github_answers


class TickRig:
    """A tick over the local board: o/r's checkout is in story.repos_root, GitHub answers, gh is recorded,
    and every pipeline the tick makes runs on `engine`."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        engine: FakeEngine | None = None,
        **story_extra: Any,
    ) -> None:
        root = tmp_path / "git"
        self.tmp = tmp_path
        self.repo = checkout_of(root, "o/r")
        self.trunk = git(self.repo, "branch", "--show-current")
        story = {
            "repos_root": str(root),
            "trunk": self.trunk,
            "plan": {"round_steps": 0},
            "layer_attempts": 1,
            **story_extra,
        }
        self.cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
        self.engine = engine or FakeEngine({"architect": []})
        self.gh = RecordedGh()
        monkeypatch.setattr(factory, "_engine", lambda cfg: self.engine)
        monkeypatch.setattr(
            factory,
            "Publisher",
            lambda *, remote, app, publish: Publisher(remote=remote, run=self.gh, app=app, publish=publish),
        )
        github_answers(monkeypatch)
        self.store = BoardStore(self.cfg.observability.state_dir / "board.sqlite")

    def epic(self, description: str | None = None, status: str = "approved") -> str:
        text = EPIC_TEXT + block(SPEC.to_dict()) if description is None else description
        key = self.store.create(level="epic", title="Avatars", description=text, labels=["repo:o/r"]).key
        self.store.update(key, status=status)  # type: ignore[arg-type]
        return key

    def stories(self, epic: str) -> list[str]:
        return [s.key for s in self.store.list(level="story", parent=epic)]

    def status(self, key: str) -> str:
        return self.store.get(key).status

    def epic_events(self, epic: str) -> Path:
        return self.cfg.observability.state_dir / "epics" / epic / "events.jsonl"


def specs_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: FakeEngine, **specs: Any) -> TickRig:
    return TickRig(
        tmp_path,
        monkeypatch,
        engine,
        specs={"enabled": True, "consultable": ["architect"], **specs},
    )
