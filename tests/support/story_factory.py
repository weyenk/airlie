"""Shared helpers extracted from tests/unit/test_story_factory.py for tests.support.story_factory."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.story import factory
from airlie.story import github_app as github_app_module
from airlie.story.board import BoardError, Card, Column
from airlie.story.state import StoryState
from tests.support.story import STORY_ROLES
from tests.support.studio import cfg_for as studio_cfg_for

# Re-exported for test modules that imported STORY_ROLES from test_story_factory before the move.
__all__ = ["STORY_ROLES"]


def cfg_for(tmp_path: Path, *, extra: dict[str, Any] | None = None, **kw: Any) -> Any:
    """The studio tests' config with the story settings that keep these tests' scripted replies in order:
    planning in one go and no survey (the rounds and the survey have tests of their own)."""
    extra = dict(extra or {})
    # no tester: it is a subtask per layer with scripted replies of its own (one test runs with it)
    extra["story"] = {
        "plan_round_steps": 0,
        "stage_attempts": 1,
        "package_steps": 1,
        "survey": False,
        "amigos": {"enabled": False},  # the story meeting has tests of its own (test_story_pipeline.py)
        **extra.get("story", {}),
    }
    kw.setdefault("done_check", False)  # it costs the coder one scripted reply per layer
    return studio_cfg_for(tmp_path, extra=extra, **kw)


class Intake:
    """A board whose intake query returns `top`; it counts how often it was asked."""

    def __init__(self, top: Card | None) -> None:
        self.top = top
        self.asked = 0

    async def card(self, ref: str) -> Card:
        raise BoardError("not needed here")

    async def next_card(self) -> Card | None:
        self.asked += 1
        return self.top

    async def move(self, card: Card, column: Column) -> None: ...

    async def comment(self, card: Card, text: str, role: str | None = None) -> None: ...


def github_answers(
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[httpx.Request], httpx.Response] = lambda request: httpx.Response(200, json={}),
) -> list[httpx.Request]:
    """GitHub's API as a tick that publishes as you pings it (github_app.ping), answering with `answer` (200
    by default); returns the requests it got. A tick that publishes as the bot asks its app instead."""
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return answer(request)

    async def ping(api_url: str) -> None:
        await github_app_module.ping(api_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(factory, "ping", ping)
    return asked


def started_log(monkeypatch: pytest.MonkeyPatch) -> list[Card]:
    started: list[Card] = []

    class Built:
        async def run(self, card: Card) -> StoryState:
            started.append(card)
            return StoryState(path=Path("s"), card=card.to_dict(), repo="r", stage="review")

        async def plan(self, card: Card) -> StoryState:
            return StoryState(path=Path("s"), card=card.to_dict(), repo="r", stage="layers")

        async def build(self, card: Card) -> StoryState:
            return await self.run(card)

    monkeypatch.setattr(factory, "make_pipeline", lambda cfg, **kw: Built())
    return started
