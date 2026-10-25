"""The story pipeline over airlie's own board (story.board: local): a story item is a Card (without airlie's
own notes, as with Jira), the oldest approved story is next (todo is where a story waits for a person to
approve it), the pipeline's columns are the board's statuses, and airlie's notes are comments by airlie."""

from __future__ import annotations

from pathlib import Path

from airlie.board import BoardStore, Item
from airlie.board.model import Comment, Status
from airlie.story.board import ISSUE_KEY, NOTE_PREFIX, BoardError, Card, Column
from airlie.story.epic_plan import after_keys, epic_key_of
from airlie.story.epic_state import epic_needs_work
from airlie.story.initiative import Initiative
from airlie.story.model import StoryError

STATUS_OF: dict[Column, str] = {
    "intake": "todo",
    "building": "doing",
    "review": "review",
    "blocked": "blocked",
}


OPEN_EPIC: tuple[Status, ...] = ("approved", "doing")
"""The statuses of an epic whose stories the tick may promote and pick. Any other (todo, blocked, review,
done) holds them: a person moving an epic to blocked or todo pauses it whole."""


class LocalBoard:
    def __init__(
        self, store: BoardStore, role_names: set[str] | None = None, epics_dir: Path | None = None
    ) -> None:
        """`epics_dir` is where the epics' records are (observability.state_dir/epics): `next_epic` reads
        them to tell an epic that still needs its stories from one that has them."""
        self.store = store
        self.role_names = frozenset(role_names or ())
        self.epics_dir = epics_dir

    def is_note(self, author: str, text: str) -> bool:
        """Airlie's and the configured roles' notes, by author or by the **airlie** · prefix (legacy)."""
        return author in self.role_names or author == "airlie" or text.startswith(NOTE_PREFIX)

    def _item(self, ref: str) -> Item:
        key = ref.strip()
        if not ISSUE_KEY.fullmatch(key):
            raise BoardError(f"not a board item key ({self.store.prefix}-7): {ref!r}")
        try:
            return self.store.get(key)
        except KeyError:
            raise BoardError(f"no item {key} on the board") from None

    def item(self, ref: str) -> Item:
        """The board item `ref` at any level; a BoardError when there is none."""
        return self._item(ref)

    def initiative(self, ref: str) -> Initiative:
        """The initiative `ref`, read from its card. A BoardError for an item of another level, or an
        initiative whose card cannot be read (no repository label, a budget that is no number)."""
        item = self._item(ref)
        try:
            return Initiative.from_item(item)
        except StoryError as e:
            raise BoardError(str(e)) from None

    def next_initiative(self) -> Item | None:
        """The oldest initiative a person has approved (P4): the specs phase takes it next."""
        found = self.store.list(level="initiative", status="approved")
        return found[0] if found else None

    def is_person(self, comment: Comment) -> bool:
        """A comment whose author is not a role or airlie, and that is no airlie note: a person's."""
        return not self.is_note(comment.author, comment.text)

    def _card(self, item: Item) -> Card:
        return Card(
            key=item.key,
            id=item.key,
            ref=item.key,
            title=item.title,
            description=item.description,
            url=None,
            comments=tuple(c.text for c in item.comments if not self.is_note(c.author, c.text)),
            labels=item.labels,
        )

    async def card(self, ref: str) -> Card:
        item = self._item(ref)
        if item.level != "story":
            an = "an" if item.level[0] in "aeiou" else "a"
            raise BoardError(f"{item.key} is {an} {item.level}; only stories are built")
        return self._card(item)

    async def next_card(self) -> Card | None:
        """The oldest story in approved, which only a person moves a story into (a cancelled one goes back to
        todo, so it waits to be approved again); epics and initiatives are never built. A story the line made
        for an epic (its `epic:` label) is passed over while its epic is held (D10 of the plans phase)."""
        for story in self.store.list(level="story", status="approved"):
            if not self.is_held(story):
                return self._card(story)
        return None

    def is_held(self, story: Item) -> bool:
        """The story was made by the line for an epic that a person holds (todo or blocked) or that is not
        open (review, done). A story with no `epic:` label, or one whose epic is gone, is never held."""
        key = epic_key_of(story.labels)
        if key is None:
            return False
        try:
            return self.store.get(key).status not in OPEN_EPIC
        except KeyError:
            return False

    def next_epic(self) -> Item | None:
        """The oldest approved epic that still needs the pipeline (no plan yet, or cards still to make): an
        epic is planned only once a person has approved it (D1 of the plans phase)."""
        for epic in self.store.list(level="epic", status="approved"):
            if self.epics_dir is None or epic_needs_work(self.epics_dir / epic.key):
                return epic
        return None

    def waiting_stories(self) -> list[Item]:
        """The stories the line made that wait on another (todo, with an `after:` label) and whose epic is not
        held: the tick promotes the ones whose dependencies have reached review."""
        return [
            s
            for s in self.store.list(level="story", status="todo")
            if after_keys(s.labels) and not self.is_held(s)
        ]

    def set_epic_status(self, key: str, status: Status) -> None:
        """Move the epic `key` to `status`; a BoardError for anything that is not an epic on the board."""
        item = self._item(key)
        if item.level != "epic":
            raise BoardError(f"{item.key} is a {item.level}, not an epic")
        self.store.update(item.key, status=status)

    def follow_stories(self) -> list[tuple[str, str, str]]:
        """Move each epic the line planned along with its stories (D10): doing once a story has been picked,
        review when every story is in review or done, done when every story is done, back to doing when a
        re-plan adds work. Only an epic that is approved, doing or review moves, so a person's hold is never
        overridden; an epic with no story of the line's is not followed. Returns (epic, from, to) per move."""
        moved: list[tuple[str, str, str]] = []
        for epic in self.store.list(level="epic"):
            if epic.status not in ("approved", "doing", "review"):
                continue
            mine = [
                s for s in self.store.list(level="story", parent=epic.key) if f"epic:{epic.key}" in s.labels
            ]
            if not mine:
                continue
            statuses = {s.status for s in mine}
            if statuses == {"done"}:
                target: Status = "done"
            elif statuses <= {"review", "done"}:
                target = "review"
            elif statuses & {"doing", "review", "done", "blocked"}:
                target = "doing"
            else:
                continue  # nothing picked yet: the epic stays as it is
            if target != epic.status:
                self.store.update(epic.key, status=target)
                moved.append((epic.key, epic.status, target))
        return moved

    def status_of(self, card: Card) -> str:
        """The column `card` is in now, as the board holds it (todo, approved, doing, …)."""
        return self._item(card.key).status

    async def move(self, card: Card, column: Column) -> None:
        self._item(card.key)
        self.store.update(card.key, status=STATUS_OF[column])  # type: ignore[arg-type]

    async def comment(self, card: Card, text: str, role: str | None = None) -> None:
        """`text` as given (the pipeline passes note(text), as it does to every board), by `role` when one is
        given, else by airlie."""
        self._item(card.key)
        self.store.comment(card.key, role or "airlie", text)
