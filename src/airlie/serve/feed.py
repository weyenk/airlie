"""The live feed: what changed, as a hint, to every open `GET /v1/events` stream. An event is a topic and an
optional key (`board` / `AIR-17`, `runs`, `jobs`); the page refetches through the existing GET routes, so the
feed carries no data and keeps no history (a client that reconnects refetches everything it shows).

`Feed.publish` is safe from any thread: the board routes are plain `def`s, which FastAPI runs in a worker
thread, while the job manager and the watcher publish from the event loop. Each subscriber has its own queue,
filled through `loop.call_soon_threadsafe` on the loop the subscriber lives in."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType

HEARTBEAT_S = 15.0
"""How long a stream may be idle before it sends a comment line: proxies close a connection that stays
silent."""


@dataclass(frozen=True)
class Change:
    id: int
    topic: str
    key: str | None


class Subscription:
    """One open stream's view of the feed: an async context manager (it counts as a subscriber only inside
    its block) whose iterator yields each `Change` published meanwhile, and ends when the feed is closed."""

    def __init__(self, feed: Feed) -> None:
        self._feed = feed
        self._queue: asyncio.Queue[Change | None] = asyncio.Queue()  # None: the feed was closed
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ended = False

    async def __aenter__(self) -> Subscription:
        self._loop = asyncio.get_running_loop()
        self._feed._attach(self)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._feed._detach(self)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Change:
        if self._ended:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is None:
            self._ended = True
            raise StopAsyncIteration
        return item

    def _deliver(self, item: Change | None) -> bool:
        """Queue `item` from any thread; False when the subscriber's loop is gone."""
        assert self._loop is not None
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:  # the loop is closed
            return False
        return True


class Feed:
    """The hub: one per app (`app.state.feed`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # ids and the subscriber set: publish runs on any thread
        self._subs: set[Subscription] = set()
        self._last = 0
        self._closed = False

    @property
    def subscribers(self) -> int:
        """How many streams are open; the watcher stops polling when it falls to 0."""
        with self._lock:
            return len(self._subs)

    def subscribe(self) -> Subscription:
        return Subscription(self)

    def publish(self, topic: str, key: str | None = None) -> None:
        """Tell every subscriber that `topic` (and `key`, when one thing in it changed) changed. Ids rise by
        one per call, and a subscriber sees them in order whichever threads publish: the id is taken and
        every queue is scheduled under one lock."""
        with self._lock:
            if self._closed:
                return
            self._last += 1
            change = Change(self._last, topic, key)
            for sub in list(self._subs):
                if not sub._deliver(change):
                    self._subs.discard(sub)  # a stream whose loop is gone cannot be told, or count

    def close(self) -> None:
        """End every subscription, after the changes already published; later subscriptions end at once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for sub in list(self._subs):
                if not sub._deliver(None):
                    self._subs.discard(sub)

    def _attach(self, sub: Subscription) -> None:
        with self._lock:
            if self._closed:
                sub._queue.put_nowait(None)  # on the subscriber's own loop: it is entering its block
            else:
                self._subs.add(sub)

    def _detach(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)


def _frame(change: Change) -> bytes:
    data = json.dumps({"topic": change.topic, "key": change.key})
    return f"id: {change.id}\nevent: change\ndata: {data}\n\n".encode()


async def event_stream(feed: Feed, *, heartbeat_s: float = HEARTBEAT_S) -> AsyncIterator[bytes]:
    """The body of `GET /v1/events` (server-sent events). It subscribes before it yields `: connected`, so
    nothing published after that line is missed; `: ping` fills each idle `heartbeat_s`. It ends when the feed
    closes, and leaves the subscriber count whether it ends, is closed or is cancelled."""
    async with feed.subscribe() as sub:
        yield b": connected\n\n"
        while True:
            try:
                change = await asyncio.wait_for(anext(sub), heartbeat_s)
            except TimeoutError:
                yield b": ping\n\n"
            except StopAsyncIteration:
                return
            else:
                yield _frame(change)
