"""The board run dispatcher: when a card is moved to Approved, run the workflow its `workflow:<name>` label
names. A card with no such label has nothing to run: it is moved to Blocked with a comment saying why.
Otherwise the card moves to Doing while the workflow runs, then to Review on success or to Blocked (with the
error as a comment) on failure.

At most `cfg.runs.max_concurrent` runs go at once, system-wide. An Approved card over the cap waits in a
FIFO queue (in the order it was approved) and stays Approved. When `workdir_for` and `can_lock` are injected,
a card whose workdir cannot be locked also stays Approved and queued, without holding up the cards behind it;
it is tried again whenever a run finishes or `pump()` is called.

`start(*, name, card, key)` returns an object with `.ok: bool` (and an optional `.error: str`); raising counts
as a failure with `str(exc)` as the error.

The start function, the publish hook and the clock are injected, so the dispatcher holds no engine, agent or
real clock of its own."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
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
        workdir_for: Callable[[dict[str, Any]], Path] | None = None,
        can_lock: Callable[[Path], bool] | None = None,
    ) -> None:
        self.board = board
        self.cfg = cfg
        self.start = start
        self.publish = publish
        self.clock = clock
        self.workdir_for = workdir_for
        self.can_lock = can_lock
        self._queue: deque[str] = deque()
        """Approved keys waiting for a slot or a free workdir, oldest first."""
        self._inflight: dict[str, asyncio.Task[None] | None] = {}
        """A key is claimed (None) before anything can await, then holds its run's task."""

    async def notify_status(self, key: str, status: str) -> None:
        """A card of this key has just been moved to `status`. Only Approved matters here."""
        if status != "approved":
            return
        if key in self._inflight or key in self._queue:
            return
        if workflow_label(self.board.get(key).labels) is None:
            self._block(key, MISSING_WORKFLOW)
            return
        self._queue.append(key)
        self._pump()

    async def pump(self) -> None:
        """Start whatever the cap and the workdir locks now allow (e.g. after a lock was released)."""
        self._pump()

    def _pump(self) -> None:
        for key in list(self._queue):
            if len(self._inflight) >= self.cfg.runs.max_concurrent:
                return
            if key in self._inflight:
                self._queue.remove(key)
                continue
            item = self.board.get(key)
            if item.status != "approved":
                self._queue.remove(key)
                continue
            name = workflow_label(item.labels)
            if name is None:
                self._queue.remove(key)
                self._block(key, MISSING_WORKFLOW)
                continue
            if not self._lockable(item.to_dict()):
                continue
            self._queue.remove(key)
            self._inflight[key] = None
            try:
                doing = self.board.move(key, "doing", by=DISPATCHER)
                self.publish("board", key)
                self._inflight[key] = asyncio.create_task(self._run(key, name, doing.to_dict()))
            except BaseException:
                self._inflight.pop(key, None)
                raise

    def _lockable(self, card: dict[str, Any]) -> bool:
        if self.workdir_for is None or self.can_lock is None:
            return True
        return self.can_lock(self.workdir_for(card))

    async def drain(self) -> None:
        """Wait until every started run has finished."""
        while tasks := [t for t in self._inflight.values() if t is not None]:
            await asyncio.gather(*tasks)

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
            self._pump()

    def _block(self, key: str, reason: str) -> None:
        self.board.move(key, "blocked", by=DISPATCHER)
        self.board.comment(key, DISPATCHER, reason)
        self.publish("board", key)
