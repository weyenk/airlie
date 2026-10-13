"""FakeEngine: scripted replies per role, records every request. The test double for all later milestones."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from airlie.engine.protocol import OnToken
from airlie.engine.types import (
    EngineCapabilities,
    EngineError,
    EngineHealth,
    EngineStats,
    GenRequest,
    GenResult,
    Message,
    TokenEvent,
    ToolCall,
)


@dataclass
class FakeReply:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    p_yes: float | None = None
    cached_tokens: int | None = None
    finish_reason: str | None = None  # override; default derives from tool_calls
    accepted_draft_tokens: int | None = None
    draft_tokens: int | None = None


def _words(text: str) -> list[str]:
    parts = text.split(" ")
    return [w + " " if i < len(parts) - 1 else w for i, w in enumerate(parts) if parts]


class FakeEngine:
    capabilities = EngineCapabilities(batching=True, prefix_cache=True, logprobs=True, native_tools=True)

    def __init__(self, scripts: dict[str, list[FakeReply]] | None = None) -> None:
        self._scripts: dict[str, deque[FakeReply]] = {r: deque(v) for r, v in (scripts or {}).items()}
        self.requests: list[GenRequest] = []
        self.cancelled: list[str] = []
        self._stats = EngineStats()
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def health(self) -> EngineHealth:
        return EngineHealth(ok=True, models={r: "ready" for r in self._scripts}, detail="fake")

    def _next(self, role: str) -> FakeReply:
        if role not in self._scripts:
            raise EngineError(f"no script for role '{role}'")
        if not self._scripts[role]:
            raise EngineError(f"script exhausted for role '{role}'")
        return self._scripts[role].popleft()

    async def generate(self, req: GenRequest, on_token: OnToken | None = None) -> GenResult:
        self.requests.append(req)
        reply = self._next(req.role)
        self._stats.requests += 1
        if on_token:
            for w in _words(reply.reasoning) if reply.reasoning else []:
                on_token(TokenEvent(text=w, reasoning=True))
            for w in _words(reply.text) if reply.text else []:
                on_token(TokenEvent(text=w))
        completion = len(reply.text.split()) + len(reply.reasoning.split())
        self._stats.tokens_out += completion
        finish = reply.finish_reason or ("tool_calls" if reply.tool_calls else "stop")
        prompt_tokens = sum(len(m.content.split()) for m in req.messages) or 1
        return GenResult(
            text=reply.text,
            reasoning=reply.reasoning,
            tool_calls=list(reply.tool_calls),
            finish_reason=finish,  # type: ignore[arg-type]
            prompt_tokens=prompt_tokens,
            completion_tokens=completion,
            cached_tokens=reply.cached_tokens,
            accepted_draft_tokens=reply.accepted_draft_tokens,
            draft_tokens=reply.draft_tokens,
            ttft_ms=1.0,
            decode_tps=1000.0,
            raw_finish=finish,
        )

    async def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)

    async def yes_no(self, role: str, messages: list[Message]) -> tuple[float, float]:
        reply = self._next(role)
        self._stats.requests += 1
        if reply.p_yes is not None:
            return reply.p_yes, 1.0 - reply.p_yes
        yes = reply.text.strip().upper().startswith("YES")
        return (1.0, 0.0) if yes else (0.0, 1.0)

    def stats(self) -> EngineStats:
        return self._stats
