"""Router seam: one closed-set decision per subtask (design 2026-10-13 §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

ROLE_QUESTION = "Which specialist should handle this task?"


@dataclass(frozen=True)
class RouteDecision:
    question: str
    choice: str | None
    chosen: str
    decision: str
    """clear | ambiguous | low-confidence | invalid | error | hint"""
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    hint: str | None = None
    input_tokens: int = 0
    detail: str = ""


class Router(Protocol):
    async def route(
        self,
        brief: str,
        criteria: dict[str, str],
        hint: str | None = None,
        *,
        question: str = "role",
        instructions: str = ROLE_QUESTION,
    ) -> RouteDecision: ...
