"""Team seam: a TeamOrchestrator runs one ReAct agent on a goal; a StepObserver sees every (u_t, o_t) step.

The observer is where Learning-to-Share plugs in (M3): `before_step` returns text to inject (shared memory
keys), `after_step` receives the step triplet for admission."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from airlie.config import RoleSpec
from airlie.engine.types import ToolCall
from airlie.scheduler import SlotScheduler
from airlie.tools import ToolRegistry

StopReason = Literal[
    "done",
    "max_steps",
    "token_budget",
    "repeated_action",
    "engine_error",
    "cancelled",
    "fork_point",
    "cut_off",
    "malformed_call",
    "no_tools",
]
"""cut_off: a reply with no tool call ended at max_tokens: not a finished answer, whatever text it holds.
malformed_call: two replies in a row held a tool call the server could not parse, left in the text.
no_tools: a role with no tools called one in two replies in a row, and so gave no answer."""


def cut_off_text(role_name: str, role: RoleSpec, *, thinking: bool = False) -> str:
    """What a person reads when a reply of `role_name` ended at max_tokens: what happened, and what to raise.
    `thinking`: the reply was all thinking (the server split it off, so there is no visible text)."""
    still = ", still thinking" if thinking else ""
    fix = f"raise roles.{role_name}.max_tokens"
    if role.thinking and role.thinking_budget is not None and role.max_tokens <= role.thinking_budget:
        fix += f" above its thinking_budget ({role.thinking_budget})"
    return f"the reply was cut off at max_tokens ({role.max_tokens}) before it ended{still}; {fix}"


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
    reasoning: str = ""
    """What the model thought before the action (a thinking role); the step trace keeps it."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    gen_ms: float = 0.0
    tool_ms: float = 0.0
    accepted_draft_tokens: int | None = None
    draft_tokens: int | None = None


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
    draft_budget: Callable[[], int | None] = field(default=lambda: None)
    """HAL budget provider (spec 04 §1): consulted before each generation request."""
    preamble: str | None = None
    """Text placed before the goal in the first user message (the repo map)."""
    initial_state: dict[str, Any] | None = None
    """A snapshot from an earlier TeamResult.state; the conversation resumes from it verbatim."""
    nudge: str | None = None
    """With initial_state: the next user message instead of the goal (branch label, hypothesis)."""
    stop_when: Callable[[StepRecord], bool] | None = None
    """Evaluated after every step; True stops the run with stop_reason="fork_point" and a state snapshot."""
    images: list[Path] = field(default_factory=list)
    """Image files attached to the goal message as content parts (vision roles)."""
    emit: Callable[..., None] | None = None
    """The run's event sink (`EventSink.emit(kind, **fields)`), for what the team sees that is not a step."""


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
    state: dict[str, Any] | None = None
    last_test_output: str = ""


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
