"""What is on the board: an Item at one of four levels, with a status, a parent one level up, labels and
comments."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, get_args

Level = Literal["initiative", "epic", "story", "subtask"]
LEVELS: tuple[Level, ...] = get_args(Level)
"""Top down: an item's parent is the level before its own; an initiative has none."""
Status = Literal["todo", "doing", "review", "blocked", "done"]
STATUSES: tuple[Status, ...] = get_args(Status)


def parent_level(level: Level) -> Level | None:
    """The level an item of `level` has its parent at: None for an initiative."""
    i = LEVELS.index(level)
    return LEVELS[i - 1] if i else None


@dataclass(frozen=True)
class Comment:
    author: str
    text: str
    at: float


@dataclass(frozen=True)
class Item:
    key: str
    """<prefix>-<n>: satisfies story.model.ISSUE_KEY, so it names a story directory as a Jira key does."""
    level: Level
    title: str
    description: str
    """Markdown."""
    status: Status
    parent: str | None
    labels: tuple[str, ...]
    """A repo:<owner>/<name> label names a story's repository, as it does for Jira."""
    comments: tuple[Comment, ...]
    created_at: float
    updated_at: float
    created_by: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["labels"] = list(self.labels)
        return d
