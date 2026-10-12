"""Team seam: a TeamOrchestrator runs one ReAct agent on a goal; a StepObserver sees every (u_t, o_t) step.

The observer is where Learning-to-Share plugs in (M3): `before_step` returns text to inject (shared memory
keys), `after_step` receives the step triplet for admission."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from airlie.config import RoleSpec
from airlie.engine.types import ToolCall
from airlie.scheduler import SlotScheduler
from airlie.tools import ToolRegistry

StopReason = Literal["done", "max_steps", "token_budget", "repeated_action", "engine_error", "cancelled"]


@dataclass
class StepRecord:
    team_id: str
    step_num: int
    u_t: ToolCall
    """The action: the tool call the agent made."""
    o_t: str
    """The observation: the tool's output."""
    ok: bool
    assistant_text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    gen_ms: float = 0.0
    tool_ms: float = 0.0


@dataclass
class TeamContext:
    team_id: str
    goal: str
    role: RoleSpec
    tools: ToolRegistry
    worktree: Path
    role_name: str = "coder"
    max_steps: int = 40
    token_budget: int = 200_000
    scheduler: SlotScheduler = field(default_factory=lambda: SlotScheduler(4))
    repeat_limit: int = 3


@dataclass
class TeamResult:
    team_id: str
    ok: bool
    stop_reason: StopReason
    final_text: str
    steps: int
    prompt_tokens: int
    completion_tokens: int
    wall_ms: float


class StepObserver(Protocol):
    async def before_step(self, team_id: str, step_num: int) -> str | None: ...
    async def after_step(self, step: StepRecord) -> None: ...


class NullObserver:
    async def before_step(self, team_id: str, step_num: int) -> str | None:
        return None

    async def after_step(self, step: StepRecord) -> None:
        return None


class TeamOrchestrator(Protocol):
    async def run(self, ctx: TeamContext, observer: StepObserver) -> TeamResult: ...
