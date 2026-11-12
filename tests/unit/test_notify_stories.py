"""Story events to pushes: which event kinds send, history is not replayed, a torn last line waits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airlie.notify.stories import StoryNotes


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def notify(
        self, user_id: str | None, kind: str, title: str, url: str, body: str | None = None
    ) -> int:
        self.calls.append((user_id, kind, title, url, body))
        return 1


def notes(tmp_path: Path) -> tuple[StoryNotes, FakeNotifier]:
    fake = FakeNotifier()
    return StoryNotes(tmp_path, fake), fake  # type: ignore[arg-type]  # the fake has Notifier's notify


def append(path: Path, event: dict[str, object] | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = event if isinstance(event, str) else json.dumps(event) + "\n"
    with path.open("a") as f:
        f.write(line)


async def test_new_lines_notify_once_and_history_is_not_replayed(tmp_path: Path) -> None:
    log = tmp_path / "stories" / "AIR-9" / "events.jsonl"
    append(log, {"kind": "blocked", "reason": "old"})
    n, fake = notes(tmp_path)
    assert await n.on_story_change("AIR-9") == 0
    append(log, {"kind": "blocked", "reason": "needs a decision"})
    append(log, {"kind": "stage", "stage": "review"})
    assert await n.on_story_change("AIR-9") == 2
    assert fake.calls == [
        (None, "story_blocked", "AIR-9 is blocked", "/board/AIR-9", "needs a decision"),
        (None, "story_published", "AIR-9 is published, review wanted", "/board/AIR-9", None),
    ]
    assert await n.on_story_change("AIR-9") == 0


async def test_a_mockups_event_sends_and_a_long_reason_is_cut(tmp_path: Path) -> None:
    log = tmp_path / "stories" / "AIR-9" / "events.jsonl"
    append(log, {"kind": "stage", "stage": "new"})
    n, fake = notes(tmp_path)
    await n.on_story_change("AIR-9")
    append(log, {"kind": "mockups"})
    append(log, {"kind": "blocked", "reason": "x" * 300})
    assert await n.on_story_change("AIR-9") == 2
    assert fake.calls[0] == (None, "story_mockups", "AIR-9 has mockups to pick", "/board/AIR-9", None)
    assert fake.calls[1][4] == "x" * 120


async def test_other_kinds_and_a_torn_last_line_send_nothing(tmp_path: Path) -> None:
    log = tmp_path / "stories" / "AIR-9" / "events.jsonl"
    append(log, {"kind": "stage", "stage": "new"})
    n, fake = notes(tmp_path)
    await n.on_story_change("AIR-9")
    append(log, {"kind": "retry"})
    append(log, {"kind": "stage", "stage": "build"})
    append(log, "not json\n")
    append(log, '{"kind": "blocked", "reaso')
    assert await n.on_story_change("AIR-9") == 0
    append(log, 'n": "late"}\n')
    assert await n.on_story_change("AIR-9") == 1
    assert fake.calls == [(None, "story_blocked", "AIR-9 is blocked", "/board/AIR-9", "late")]


async def test_a_story_with_no_log_sends_nothing(tmp_path: Path) -> None:
    n, fake = notes(tmp_path)
    assert await n.on_story_change("AIR-1") == 0
    assert fake.calls == []
