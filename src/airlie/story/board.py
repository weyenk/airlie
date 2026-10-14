"""Where stories come from and where their progress shows: the Board seam (a Jira project today), a NullBoard
for `--text` requests, and the Card a story is keyed by."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from airlie.story.model import ISSUE_KEY as ISSUE_KEY  # defined with the other story types; exported here too

Column = Literal["building", "review", "blocked"]
NOTE_PREFIX = "**airlie** · "
_KEY = ISSUE_KEY.pattern
_BROWSE = re.compile(rf"https?://[^/\s]+/browse/({_KEY})(?:[/?#]\S*)?", re.ASCII)  # ASCII: as ISSUE_KEY
_SELECTED = re.compile(rf"https?://\S*[?&]selectedIssue=({_KEY})(?:[&#]\S*)?", re.ASCII)


def note(text: str) -> str:
    """A comment written by airlie; the prefix keeps airlie's notes out of the next story brief."""
    return NOTE_PREFIX + text


class BoardError(RuntimeError):
    """The board cannot be read or written: auth, a missing status, a bad issue reference."""


@dataclass(frozen=True)
class Card:
    key: str
    """Stable and filesystem-safe (the issue key, or text-<hash>); names the story's directory."""
    id: str
    """The board's own id (Jira's numeric issue id)."""
    ref: str
    """What a person types to name it (the issue key, AIR-7); "" for a written request. It prefixes branch
    names."""
    title: str
    description: str
    url: str | None = None
    comments: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def resume(self) -> str:
        return f"`task story -- {self.ref}`" if self.ref else "the same `task story -- --text ...` command"


def text_card(text: str) -> Card:
    """A card for a written request (no board); the same text resumes the same story."""
    body = text.strip()
    key = "text-" + hashlib.sha256(body.encode()).hexdigest()[:10]
    title = body.splitlines()[0][:80] if body else "request"
    return Card(key, key, "", title, body)


class Board(Protocol):
    async def card(self, ref: str) -> Card: ...

    async def next_card(self) -> Card | None: ...

    async def move(self, card: Card, column: Column) -> None: ...

    async def comment(self, card: Card, text: str) -> None: ...


class NullBoard:
    """No board: moves and notes go to the story's notes.md."""

    def __init__(self, stories_dir: Path) -> None:
        self.stories_dir = stories_dir

    async def card(self, ref: str) -> Card:
        raise BoardError(
            "no Jira project is configured (ATLASSIAN_BASE_URL and ATLASSIAN_PROJECT_KEY, or story.jira); "
            "pass the request with --text"
        )

    async def next_card(self) -> Card | None:
        return None

    async def move(self, card: Card, column: Column) -> None:
        self._note(card, f"moved to {column}")

    async def comment(self, card: Card, text: str) -> None:
        self._note(card, text)

    def _note(self, card: Card, text: str) -> None:
        path = self.stories_dir / card.key / "notes.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def parse_card_ref(ref: str) -> str:
    """An issue key (AIR-7), its browse link (https://SITE/browse/AIR-7) or a board link with the issue open
    (...?selectedIssue=AIR-7) → the issue key."""
    ref = ref.strip()
    if ISSUE_KEY.fullmatch(ref):
        return ref
    for link in (_BROWSE, _SELECTED):
        m = link.fullmatch(ref)
        if m:
            return m.group(1)
    raise BoardError(f"not a Jira issue key (AIR-7) or issue link (https://SITE/browse/AIR-7): {ref!r}")
