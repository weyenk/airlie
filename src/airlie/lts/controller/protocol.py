"""Admission controller seam (spec 03 §2, 04 §3 `evaluate_lts_admission`): one binary decision per step."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from airlie.engine.types import ToolCall


@dataclass
class AdmissionContext:
    query: str
    memory_keys: list[str]
    u_t: ToolCall
    s_t: str
    o_t: str
    team_id: str
    step_num: int
    ok: bool = True
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    admit: bool
    p_yes: float
    reason: str


class LTSAdmissionController(Protocol):
    kind: str

    async def decide(self, ctx: AdmissionContext) -> Decision: ...
