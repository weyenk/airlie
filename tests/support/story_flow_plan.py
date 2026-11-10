"""Shared helpers extracted from tests/unit/test_story_flow_plan.py for tests.support.story_flow_plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.support.story_flow import KEY, write_events


def make_story(root: Path, events: list[dict[str, Any]], **state: Any) -> Path:
    """Story KEY under `root` with `state` over a blank one at stage `new`, and `events` as its log."""
    story = root / "stories" / KEY
    story.mkdir(parents=True)
    base: dict[str, Any] = {"card": {"key": KEY, "title": "t"}, "repo": "/r", "stage": "new", "layers": []}
    (story / "state.json").write_text(json.dumps({**base, **state}))
    write_events(story / "events.jsonl", events)
    return root
