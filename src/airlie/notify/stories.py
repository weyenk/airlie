"""A story that is blocked, published or waiting on a mockup pick sends a push: the lines appended to its
`stories/<key>/events.jsonl` since the last look, one push at most per event."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from airlie.notify.kinds import NotificationKind
from airlie.notify.notifier import Notifier

log = logging.getLogger("airlie.notify")

REASON_CHARS = 120


class StoryNotes:
    def __init__(self, state_dir: Path, notifier: Notifier) -> None:
        self._state = state_dir
        self._notifier = notifier
        self._offsets: dict[str, int] = {}  # key: bytes of its event log already read

    async def on_story_change(self, key: str) -> int:
        """Push for the events appended since the last call for `key`; the count sent. The first call for a
        key records where its log ends and sends nothing, so a restart does not replay history. A last line
        with no newline yet is still being written and waits for the next call."""
        path = self._state / "stories" / key / "events.jsonl"
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        start = self._offsets.get(key)
        if start is None or size < start:  # first sight, or the log was replaced by a shorter one
            self._offsets[key] = size
            return 0
        if size == start:
            return 0
        try:
            with path.open("rb") as f:
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return 0
        end = data.rfind(b"\n") + 1
        self._offsets[key] = start + end
        sent = 0
        for raw in data[:end].splitlines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            note = _note(key, event)
            if note is not None:
                kind, title, body = note
                sent += await self._notifier.notify(None, kind, title, f"/board/{key}", body)
        return sent


def _note(key: str, event: dict[str, object]) -> tuple[NotificationKind, str, str | None] | None:
    match event.get("kind"):
        case "blocked":
            reason = event.get("reason")
            body = reason[:REASON_CHARS] if isinstance(reason, str) and reason else None
            return "story_blocked", f"{key} is blocked", body
        case "stage" if event.get("stage") == "review":
            return "story_published", f"{key} is published, review wanted", None
        case "mockups":
            return "story_mockups", f"{key} has mockups to pick", None
    return None
