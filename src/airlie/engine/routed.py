"""RoutedEngine: one Engine over several, each role's requests going to the engine that serves its model.

An mlx_vlm engine holds one model, so a config whose roles use several models runs several engines; the
orchestrator still holds one Engine, and this routes by role."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from airlie.engine.protocol import Engine, OnToken
from airlie.engine.types import (
    EngineCapabilities,
    EngineHealth,
    EngineStats,
    GenRequest,
    GenResult,
    Message,
)


class RoutedEngine:
    def __init__(
        self, primary: str, engines: Mapping[str, Engine], engine_for_role: Callable[[str], str]
    ) -> None:
        self._primary = primary
        self._engines = dict(engines)
        self._engine_for_role = engine_for_role
        self.capabilities: EngineCapabilities = self._engines[primary].capabilities
        self._taken_by: dict[str, Engine] = {}  # request id -> the engine generating it

    async def start(self) -> None:
        await asyncio.gather(*(e.start() for e in self._engines.values()))

    async def stop(self) -> None:
        await asyncio.gather(*(e.stop() for e in self._engines.values()))

    async def health(self) -> EngineHealth:
        return await self._engines[self._primary].health()

    async def generate(self, req: GenRequest, on_token: OnToken | None = None) -> GenResult:
        engine = self._engines[self._engine_for_role(req.role)]
        self._taken_by[req.request_id] = engine
        try:
            return await engine.generate(req, on_token)
        finally:
            self._taken_by.pop(req.request_id, None)

    async def cancel(self, request_id: str) -> None:
        taken = self._taken_by.get(request_id)
        if taken is not None:
            await taken.cancel(request_id)
        else:  # not (or no longer) in flight here: a pre-cancel is remembered by whichever engine gets it
            await asyncio.gather(*(e.cancel(request_id) for e in self._engines.values()))

    async def yes_no(self, role: str, messages: list[Message]) -> tuple[float, float]:
        return await self._engines[self._engine_for_role(role)].yes_no(role, messages)

    def stats(self) -> EngineStats:
        parts = [e.stats() for e in self._engines.values()]
        return EngineStats(
            requests=sum(s.requests for s in parts),
            tokens_out=sum(s.tokens_out for s in parts),
            errors=sum(s.errors for s in parts),
        )
