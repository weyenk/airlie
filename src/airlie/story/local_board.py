"""The story pipeline over airlie's own board (story.board: local): a story item is a Card, the oldest
story in todo is next, the pipeline's columns are the board's statuses, and airlie's notes are comments by
airlie."""

from __future__ import annotations

from airlie.board import BoardStore, Item
from airlie.story.board import ISSUE_KEY, BoardError, Card, Column, note

STATUS_OF: dict[Column, str] = {
    "intake": "todo",
    "building": "doing",
    "review": "review",
    "blocked": "blocked",
}


class LocalBoard:
    def __init__(self, store: BoardStore) -> None:
        self.store = store

    def _item(self, ref: str) -> Item:
        key = ref.strip()
        if not ISSUE_KEY.fullmatch(key):
            raise BoardError(f"not a board item key ({self.store.prefix}-7): {ref!r}")
        try:
            return self.store.get(key)
        except KeyError:
            raise BoardError(f"no item {key} on the board") from None

    @staticmethod
    def _card(item: Item) -> Card:
        return Card(
            key=item.key,
            id=item.key,
            ref=item.key,
            title=item.title,
            description=item.description,
            url=None,
            comments=tuple(c.text for c in item.comments),
            labels=item.labels,
        )

    async def card(self, ref: str) -> Card:
        item = self._item(ref)
        if item.level != "story":
            an = "an" if item.level[0] in "aeiou" else "a"
            raise BoardError(f"{item.key} is {an} {item.level}; only stories are built")
        return self._card(item)

    async def next_card(self) -> Card | None:
        """The oldest story in todo; epics and initiatives are never built."""
        stories = self.store.list(level="story", status="todo")
        return self._card(stories[0]) if stories else None

    async def move(self, card: Card, column: Column) -> None:
        self._item(card.key)
        self.store.update(card.key, status=STATUS_OF[column])  # type: ignore[arg-type]

    async def comment(self, card: Card, text: str) -> None:
        self._item(card.key)
        self.store.comment(card.key, "airlie", note(text))
