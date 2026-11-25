"""Long-lived update poller.

The feed side of the panel is otherwise frozen — the ticker (or `task update check`) is what
fills it. It runs one check as soon as it starts, so a boot answers "what is available" without
waiting a full interval, then waits the interval between passes.

A check is a ledger row, never a crash of the process that is serving: the panel and the CLI read
whatever the last pass left behind, so a down feed during a poll is a reason string, not an outage.
``stop`` is an event rather than a bare cancel so shutdown does not sit in ``sleep`` for the rest of
a day-long interval.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)


class UpdateTicker:
    def __init__(self, checker_coro: Callable[[], Awaitable[Any]], interval_s: float) -> None:
        self._checker_coro = checker_coro
        self._interval_s = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Idempotent: a ticker already in flight is not started twice."""
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        self._stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._checker_coro()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("update poll failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
