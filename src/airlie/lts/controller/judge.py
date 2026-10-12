"""JudgeController: a small model answers YES/NO through `Engine.yes_no` (paper framing, no training)."""

from __future__ import annotations

from airlie.engine.protocol import Engine
from airlie.engine.types import EngineError, Message
from airlie.lts.controller.protocol import AdmissionContext, Decision

PROMPT = """Task: {query}

Shared memory already holds these keys:
{keys}

A team just took this step:
- action: {tool}({args})
- summary: {summary}
- output (truncated):
{output}

Would storing this step's output in shared memory save another team working on the same task real effort,
and is it not already covered by an existing key? Answer YES or NO."""


class JudgeController:
    kind = "judge"

    def __init__(self, engine: Engine, role: str = "judge", threshold: float = 0.5) -> None:
        self.engine = engine
        self.role = role
        self.threshold = threshold

    async def decide(self, ctx: AdmissionContext) -> Decision:
        keys = "\n".join(f"- {k}" for k in ctx.memory_keys) or "(none)"
        text = PROMPT.format(
            query=ctx.query,
            keys=keys,
            tool=ctx.u_t.name,
            args=ctx.u_t.arguments[:300],
            summary=ctx.s_t,
            output=ctx.o_t[:1500],
        )
        try:
            p_yes, _ = await self.engine.yes_no(self.role, [Message(role="user", content=text)])
        except EngineError as e:
            return Decision(False, 0.0, f"judge_error: {e}")
        return Decision(p_yes >= self.threshold, p_yes, "judge")
