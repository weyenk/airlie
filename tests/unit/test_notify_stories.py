"""Event logs to pushes: which event kinds send, history is not replayed, a torn last line waits; the
stories', initiatives' and epics' logs all count."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.notify.stories import StoryNotes
from airlie.story.epic_state import EpicState
from airlie.story.initiative_state import InitiativeState


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
    n.prime()
    assert await n.on_log_change("stories", "AIR-9") == 0
    append(log, {"kind": "blocked", "reason": "needs a decision"})
    append(log, {"kind": "stage", "stage": "review"})
    assert await n.on_log_change("stories", "AIR-9") == 2
    assert fake.calls == [
        (None, "story_blocked", "AIR-9 is blocked", "/board/AIR-9", "needs a decision"),
        (None, "story_published", "AIR-9 is published, review wanted", "/board/AIR-9", None),
    ]
    assert await n.on_log_change("stories", "AIR-9") == 0


async def test_a_mockups_event_sends_and_a_long_reason_is_cut(tmp_path: Path) -> None:
    log = tmp_path / "stories" / "AIR-9" / "events.jsonl"
    append(log, {"kind": "stage", "stage": "new"})
    n, fake = notes(tmp_path)
    n.prime()
    append(log, {"kind": "mockups"})
    append(log, {"kind": "blocked", "reason": "x" * 300})
    assert await n.on_log_change("stories", "AIR-9") == 2
    assert fake.calls[0] == (None, "story_mockups", "AIR-9 has mockups to pick", "/board/AIR-9", None)
    assert fake.calls[1][4] == "x" * 120


async def test_other_kinds_and_a_torn_last_line_send_nothing(tmp_path: Path) -> None:
    log = tmp_path / "stories" / "AIR-9" / "events.jsonl"
    append(log, {"kind": "stage", "stage": "new"})
    n, fake = notes(tmp_path)
    n.prime()
    append(log, {"kind": "retry"})
    append(log, {"kind": "stage", "stage": "build"})
    append(log, "not json\n")
    append(log, '{"kind": "blocked", "reaso')
    assert await n.on_log_change("stories", "AIR-9") == 0
    append(log, 'n": "late"}\n')
    assert await n.on_log_change("stories", "AIR-9") == 1
    assert fake.calls == [(None, "story_blocked", "AIR-9 is blocked", "/board/AIR-9", "late")]


async def test_a_story_with_no_log_sends_nothing(tmp_path: Path) -> None:
    n, fake = notes(tmp_path)
    assert await n.on_log_change("stories", "AIR-1") == 0
    assert fake.calls == []


async def test_after_prime_a_new_story_and_an_existing_one_both_notify_from_their_first_event(
    tmp_path: Path,
) -> None:
    old = tmp_path / "stories" / "AIR-1" / "events.jsonl"
    append(old, {"kind": "blocked", "reason": "before"})
    n, fake = notes(tmp_path)
    n.prime()
    append(old, {"kind": "blocked", "reason": "first after"})
    new = tmp_path / "stories" / "AIR-2" / "events.jsonl"
    append(new, {"kind": "stage", "stage": "review"})
    assert await n.on_log_change("stories", "AIR-1") == 1
    assert await n.on_log_change("stories", "AIR-2") == 1
    assert [c[2] for c in fake.calls] == ["AIR-1 is blocked", "AIR-2 is published, review wanted"]
    assert fake.calls[0][4] == "first after"


async def test_a_read_is_capped_and_the_rest_comes_on_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("airlie.notify.stories.READ_BYTES", 80)
    log = tmp_path / "stories" / "AIR-9" / "events.jsonl"
    n, fake = notes(tmp_path)
    n.prime()
    for _ in range(5):
        append(log, {"kind": "mockups"})  # 19 bytes a line
    assert await n.on_log_change("stories", "AIR-9") == 4  # 4 whole lines fit in 80 bytes
    assert await n.on_log_change("stories", "AIR-9") == 1
    assert await n.on_log_change("stories", "AIR-9") == 0


async def test_an_initiative_mockups_and_blocked_and_an_epic_blocked_push_with_their_own_board_url(
    tmp_path: Path,
) -> None:
    initiative = InitiativeState(path=tmp_path / "initiatives" / "AIR-7", card={}, repos=[])
    epic = EpicState(path=tmp_path / "epics" / "AIR-8", card={}, repo="o/r")
    initiative.emit("initiative_start", card="old")
    epic.emit("blocked", reason="history")
    n, fake = notes(tmp_path)
    n.prime()
    initiative.emit("mockups", epic="e1", options=["option-1.html"], directions=["a"])
    initiative.emit("blocked", reason="needs a decision")
    epic.emit("blocked", reason="plan unusable")
    assert await n.on_log_change("initiatives", "AIR-7") == 2
    assert await n.on_log_change("epics", "AIR-8") == 1
    assert fake.calls == [
        (None, "story_mockups", "AIR-7 has mockups to pick", "/board/AIR-7", None),
        (None, "story_blocked", "AIR-7 is blocked", "/board/AIR-7", "needs a decision"),
        (None, "story_blocked", "AIR-8 is blocked", "/board/AIR-8", "plan unusable"),
    ]
    assert await n.on_log_change("initiatives", "AIR-7") == 0


async def test_a_story_and_an_epic_with_one_key_keep_separate_offsets(tmp_path: Path) -> None:
    append(tmp_path / "stories" / "AIR-1" / "events.jsonl", {"kind": "blocked", "reason": "s"})
    n, fake = notes(tmp_path)
    n.prime()
    EpicState(path=tmp_path / "epics" / "AIR-1", card={}, repo="o/r").emit("blocked", reason="e")
    assert await n.on_log_change("stories", "AIR-1") == 0
    assert await n.on_log_change("epics", "AIR-1") == 1
