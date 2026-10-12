"""The one seam between the orchestrator and any inference engine (spec 01 §2: HTTP/SSE OpenAI link)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from airlie.engine.types import (
    EngineCapabilities,
    EngineHealth,
    EngineStats,
    GenRequest,
    GenResult,
    Message,
    TokenEvent,
)

OnToken = Callable[[TokenEvent], None]


class Engine(Protocol):
    capabilities: EngineCapabilities

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> EngineHealth: ...
    async def generate(self, req: GenRequest, on_token: OnToken | None = None) -> GenResult: ...
    async def cancel(self, request_id: str) -> None: ...
    async def yes_no(self, role: str, messages: list[Message]) -> tuple[float, float]:
        """(p_yes, p_no) for a one-word YES/NO answer from `role`; used by the judge admission controller."""
        ...

    def stats(self) -> EngineStats: ...
