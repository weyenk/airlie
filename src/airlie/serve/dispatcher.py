"""The board run dispatcher: when a card is moved to Approved, run the workflow its `workflow:<name>` label
names. A card with no such label has nothing to run: it is moved to Blocked with a comment saying why.

The start function, the publish hook and the clock are injected, so the dispatcher holds no engine, agent or
real clock of its own."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from airlie.board import BoardStore
from airlie.config.schema import RunsSpec
from airlie.story.workflows import workflow_label

DISPATCHER = "dispatcher"
MISSING_WORKFLOW = "missing workflow"


class _Config(Protocol):
    @property
    def runs(self) -> RunsSpec: ...


class RunDispatcher:
    def __init__(
        self,
        *,
        board: BoardStore,
        cfg: _Config,
        start: Callable[..., Awaitable[object]],
        publish: Callable[[str, str], None],
        clock: Callable[[], float],
    ) -> None:
        self.board = board
        self.cfg = cfg
        self.start = start
        self.publish = publish
        self.clock = clock

    async def notify_status(self, key: str, status: str) -> None:
        """A card of this key has just been moved to `status`. Only Approved matters here."""
        if status != "approved":
            return
        item = self.board.get(key)
        if workflow_label(item.labels) is None:
            self._block(key, MISSING_WORKFLOW)

    def _block(self, key: str, reason: str) -> None:
        self.board.move(key, "blocked", by=DISPATCHER)
        self.board.comment(key, DISPATCHER, reason)
