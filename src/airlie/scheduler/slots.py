"""Slot accounting with cooperative suspend-and-yield (spec 04 §2).

A team holds a slot only while a generation request is in flight. Entering a tool call releases the slot
(`SUSPENDED_TOOL_STALL`); the engine's prefix cache keeps the team's KV state, so resuming costs no
re-prefill."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from airlie.scheduler.states import AgentState


@dataclass
class SlotMetrics:
    yield_count: int = 0
    max_concurrent: int = 0
    slot_wait_ms_total: float = 0.0
    generations: int = 0
    per_team_wait_ms: dict[str, float] = field(default_factory=dict)


class SlotScheduler:
    def __init__(self, max_active: int, starvation_ms: int = 30000) -> None:
        self.max_active = max_active
        self.starvation_ms = starvation_ms
        self._sem = asyncio.Semaphore(max_active)
        self._active = 0
        self._states: dict[str, AgentState] = {}
        self._metrics = SlotMetrics()

    def state(self, team_id: str) -> AgentState:
        return self._states.get(team_id, AgentState.ACTIVE)

    def active_ids(self) -> list[str]:
        return [t for t, s in self._states.items() if s == AgentState.ACTIVE]

    def metrics(self) -> SlotMetrics:
        return self._metrics

    @asynccontextmanager
    async def generation(self, team_id: str) -> AsyncIterator[None]:
        t0 = time.monotonic()
        await self._sem.acquire()
        waited = (time.monotonic() - t0) * 1000.0
        self._metrics.slot_wait_ms_total += waited
        self._metrics.per_team_wait_ms[team_id] = self._metrics.per_team_wait_ms.get(team_id, 0.0) + waited
        self._states[team_id] = AgentState.ACTIVE
        self._active += 1
        self._metrics.max_concurrent = max(self._metrics.max_concurrent, self._active)
        self._metrics.generations += 1
        try:
            yield
        finally:
            self._active -= 1
            self._sem.release()

    @asynccontextmanager
    async def tool_stall(self, team_id: str) -> AsyncIterator[None]:
        """The yield protocol: the team holds no slot while its tool runs."""
        self._states[team_id] = AgentState.SUSPENDED_TOOL_STALL
        self._metrics.yield_count += 1
        try:
            yield
        finally:
            self._states[team_id] = AgentState.ACTIVE

    def starving(self, team_id: str) -> bool:
        return self._metrics.per_team_wait_ms.get(team_id, 0.0) > self.starvation_ms
