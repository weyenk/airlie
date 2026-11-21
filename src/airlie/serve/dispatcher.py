"""The board run dispatcher: when a card is moved to Approved, run the workflow its `workflow:<name>` label
names. A card with no such label has nothing to run: it is moved to Blocked with a comment saying why.
Otherwise the card moves to Doing while the workflow runs, then to Review on success or to Blocked (with the
error as a comment) on failure.

`start(*, name, card, key)` returns an object with `.ok: bool` (and an optional `.error: str`); raising counts
as a failure with `str(exc)` as the error.

The start function, the publish hook and the clock are injected, so the dispatcher holds no engine, agent or
real clock of its own."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, Protocol

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
        self._inflight: dict[str, asyncio.Task[None]] = {}

    async def notify_status(self, key: str, status: str) -> None:
        """A card of this key has just been moved to `status`. Only Approved matters here."""
        if status != "approved":
            return
        if key in self._inflight:
            return
        item = self.board.get(key)
        name = workflow_label(item.labels)
        if name is None:
            self._block(key, MISSING_WORKFLOW)
            return
        self.board.move(key, "doing", by=DISPATCHER)
        self.publish("board", key)
        self._inflight[key] = asyncio.create_task(self._run(key, name, asdict(item)))

    async def drain(self) -> None:
        """Wait until every started run has finished."""
        while self._inflight:
            await asyncio.gather(*self._inflight.values())

    async def _run(self, key: str, name: str, card: dict[str, Any]) -> None:
        try:
            try:
                result = await self.start(name=name, card=card, key=key)
            except Exception as exc:
                self._block(key, str(exc) or type(exc).__name__)
                return
            if getattr(result, "ok", False):
                self.board.move(key, "review", by=DISPATCHER)
                self.publish("board", key)
            else:
                self._block(key, getattr(result, "error", None) or "workflow failed")
        finally:
            self._inflight.pop(key, None)

    def _block(self, key: str, reason: str) -> None:
        self.board.move(key, "blocked", by=DISPATCHER)
        self.board.comment(key, DISPATCHER, reason)
        self.publish("board", key)
