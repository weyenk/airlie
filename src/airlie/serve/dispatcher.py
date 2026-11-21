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

Airlie chat outranks board work: `request_chat_slot(session_id)` returns a `ChatLease` that holds one of the
slots until `release()`. A chat goes ahead of every queued card. When no slot is free it pauses a Doing board
run (the newest) through the injected `pause_run(key)`, which keeps the card in Doing with a "paused for chat"
comment; a chat is never paused in favour of another chat, so a chat that finds only chats waits its turn. A
paused run keeps its place: nothing queued starts ahead of it, and `resume_run(key)` is awaited as soon as
a slot is free again (when a lease is released or a run finishes). Without `pause_run` a chat just waits.

The start, pause and resume functions, the publish hook and the clock are injected, so the dispatcher holds no
engine, agent or real clock of its own."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from airlie.board import BoardStore
from airlie.config.schema import RunsSpec
from airlie.story.workflows import workflow_label

DISPATCHER = "dispatcher"
MISSING_WORKFLOW = "missing workflow"
PAUSED_FOR_CHAT = "paused for chat"
RESUMED = "resumed after chat"

log = logging.getLogger("airlie.serve.dispatcher")


class _Config(Protocol):
    @property
    def runs(self) -> RunsSpec: ...


@dataclass(eq=False)
class ChatLease:
    """One slot held for a chat turn. `release()` (idempotent) frees it and lets paused work resume.
    It is also an async context manager."""

    session_id: str
    _dispatcher: RunDispatcher = field(repr=False)
    released: bool = False

    async def release(self) -> None:
        await self._dispatcher.release_chat_slot(self)

    async def __aenter__(self) -> ChatLease:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()


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
        pause_run: Callable[[str], Awaitable[object]] | None = None,
        resume_run: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self.board = board
        self.cfg = cfg
        self.start = start
        self.publish = publish
        self.clock = clock
        self.workdir_for = workdir_for
        self.can_lock = can_lock
        self.pause_run = pause_run
        self.resume_run = resume_run
        self._queue: deque[str] = deque()
        """Approved keys waiting for a slot or a free workdir, oldest first."""
        self._inflight: dict[str, asyncio.Task[None] | None] = {}
        """A key is claimed (None) before anything can await, then holds its run's task."""
        self._paused: deque[str] = deque()
        """In-flight keys paused for chat, oldest first; each still holds its slot's place."""
        self._leases: list[ChatLease] = []
        self._chat_waiters: deque[tuple[str, asyncio.Future[ChatLease]]] = deque()
        self._resuming: set[asyncio.Task[None]] = set()
        self._resuming_keys: set[str] = set()
        """Keys whose `resume_run` has not returned yet; not pausable until it has."""

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

    def _running(self) -> int:
        return len(self._inflight) - len(self._paused)

    def _pump(self) -> None:
        self._grant_chats()
        self._resume_paused()
        for key in list(self._queue):
            # a paused run and a chat each keep their place, so nothing queued gets in ahead of them
            if len(self._inflight) + len(self._leases) >= self.cfg.runs.max_concurrent:
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
        while tasks := [t for t in (*self._inflight.values(), *self._resuming) if t is not None]:
            await asyncio.gather(*tasks)

    async def request_chat_slot(self, session_id: str) -> ChatLease:
        """Claim a slot for a chat turn, ahead of every queued card. At the cap it pauses a Doing board run;
        if only chats hold the slots, it waits (behind chats already waiting) for one to be released."""
        if not self._chat_waiters and self._running() + len(self._leases) < self.cfg.runs.max_concurrent:
            return self._lease(session_id)
        victim = None if self._chat_waiters else self._victim()
        if victim is None:
            future: asyncio.Future[ChatLease] = asyncio.get_running_loop().create_future()
            waiter = (session_id, future)
            self._chat_waiters.append(waiter)
            try:
                return await future
            except asyncio.CancelledError:
                if waiter in self._chat_waiters:
                    self._chat_waiters.remove(waiter)
                elif future.done() and not future.cancelled():
                    await self.release_chat_slot(future.result())
                raise
        lease = self._lease(session_id)
        self._paused.append(victim)  # claimed before anything can await
        try:
            if self.pause_run is not None:
                await self.pause_run(victim)
        except BaseException:
            if victim in self._paused:
                self._paused.remove(victim)
            self._leases.remove(lease)
            lease.released = True
            self._pump()
            raise
        if victim in self._paused:  # a run that finished while being paused needs no note
            self.board.comment(victim, DISPATCHER, PAUSED_FOR_CHAT)
            self.publish("board", victim)
        return lease

    async def release_chat_slot(self, lease: ChatLease) -> None:
        """Give a chat's slot back: the next waiting chat gets it, else paused runs resume, else the queue."""
        if lease.released:
            return
        lease.released = True
        self._leases.remove(lease)
        self._pump()
        if self._resuming:
            await asyncio.gather(*self._resuming)

    def _lease(self, session_id: str) -> ChatLease:
        lease = ChatLease(session_id, self)
        self._leases.append(lease)
        return lease

    def _victim(self) -> str | None:
        """The newest Doing board run still going; None when there is no pause hook or nothing to pause."""
        if self.pause_run is None:
            return None
        for key, task in reversed(self._inflight.items()):
            if task is not None and key not in self._paused and key not in self._resuming_keys:
                return key
        return None

    def _grant_chats(self) -> None:
        while self._chat_waiters and self._running() + len(self._leases) < self.cfg.runs.max_concurrent:
            session_id, future = self._chat_waiters.popleft()
            if not future.done():
                future.set_result(self._lease(session_id))

    def _resume_paused(self) -> None:
        """Resume paused runs, oldest first, while a slot is free and no chat is waiting for it."""
        while (
            self._paused
            and not self._chat_waiters
            and self._running() + len(self._leases) < self.cfg.runs.max_concurrent
        ):
            key = self._paused.popleft()
            self._resuming_keys.add(key)
            task = asyncio.create_task(self._resume(key))
            self._resuming.add(task)
            task.add_done_callback(self._resuming.discard)

    async def _resume(self, key: str) -> None:
        try:
            try:
                if self.resume_run is not None:
                    await self.resume_run(key)
            finally:
                self._resuming_keys.discard(key)
        except Exception as exc:
            log.exception("resuming %s failed", key)
            self._paused.appendleft(key)  # still Doing, still holding its place; retried on the next pump
            self.board.comment(key, DISPATCHER, f"resume failed: {exc}")
            self.publish("board", key)
            return
        self.board.comment(key, DISPATCHER, RESUMED)
        self.publish("board", key)

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
            if key in self._paused:
                self._paused.remove(key)
            self._pump()

    def _block(self, key: str, reason: str) -> None:
        self.board.move(key, "blocked", by=DISPATCHER)
        self.board.comment(key, DISPATCHER, reason)
        self.publish("board", key)
