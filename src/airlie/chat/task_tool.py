"""The `task` tool of a repository chat: a subagent with a fresh context on a role the model chooses, working
in the session's VM. It gets the role's own prompt and the one prompt it was given, never the chat's history.
Its tools are the chat's VM tools (the same `ctx`, so the same VM and `box_env`) minus the ones that reach
past the VM or the person: it has no `task` (depth 1), no `push_branch` or `open_pr`, no `skill` or `todo`,
and no tool the kind asks the person about first, so no confirmation can be reached from it. Its model calls
go through `engine_for(role)`: in serve a managed engine, so each call holds the role's engine (a reviewer
starts Muse on demand, and waits for memory with the reason shown)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

from airlie.chat.hooks import build_system_prompt
from airlie.chat.kinds import needs_yes
from airlie.chat.superpowers import SuperpowersLibrary
from airlie.config.schema import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.engine.types import EngineError, GenRequest, Message, ToolCall
from airlie.skills.library import Skill, prompt_text
from airlie.tools.loop import generate_with_tools
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import BOXED, ToolRegistry

NOT_FOR_SUBAGENTS = frozenset({"task", "push_branch", "open_pr", "skill", "todo"})
NO_ANSWER = "(the subagent gave no answer)"


@dataclass(frozen=True)
class TaskObserver:
    """What the chat turn wants to hear from a running task: each tool call the subagent made (for the audit
    log). The turn sets it in `OBSERVER` around the `task` call."""

    after_call: Callable[[ToolCall, ToolResult], Awaitable[None]]


OBSERVER: ContextVar[TaskObserver | None] = ContextVar("task_observer", default=None)


class _Observed(ToolRegistry):
    """A registry that reports each call it ran to `observer`."""

    observer: TaskObserver | None = None

    async def call(self, call: ToolCall) -> ToolResult:
        result = await super().call(call)
        if self.observer is not None:
            await self.observer.after_call(call, result)
        return result


def subagent_tools(cfg: AirlieConfig) -> list[str]:
    """The repository kind's VM tools a subagent may use: not the ones it must not reach, and not any the
    kind needs a person's yes for (a `Confirmed` tool cannot be answered from inside a subagent)."""
    asked = needs_yes("repository", cfg)
    names = cfg.chat.kinds.repository.tools
    return [n for n in names if n in BOXED and n not in NOT_FOR_SUBAGENTS and n not in asked]


class Task:
    name = "task"

    def __init__(
        self,
        cfg: AirlieConfig,
        engine_for: Callable[[str], Engine],
        *,
        library: SuperpowersLibrary | None = None,
        chat_skills: Mapping[str, Skill] | None = None,
    ) -> None:
        self._cfg = cfg
        self._engine_for = engine_for
        self._library = library
        self._chat_skills: Mapping[str, Skill] = chat_skills or {}
        spec = cfg.chat.task
        self.roles = [r for r in spec.roles if r in cfg.roles]
        self.default_role = spec.default_role
        self.engines = sorted({cfg.engine_for_role(r) for r in self.roles})
        """The engines its roles run on: the turn relays their manager status while a task runs."""
        self.description = (
            "Run a subagent with a fresh context on a role, in this VM, and get its final answer back. It "
            "sees only the prompt you give it (write it so it stands alone: what to do, which files, what to "
            "return), has the file, shell and git tools but cannot push, open a pull request or start "
            f"another task. Roles: {', '.join(self.roles)}; none means {self.default_role}."
        )
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": self.roles, "description": "the role to run the task as"},
                "prompt": {"type": "string", "description": "the task, complete in itself"},
            },
            "required": ["prompt"],
        }

    @property
    def usable(self) -> bool:
        """A task needs a role to run on, and the default must be one of them."""
        return bool(self.roles) and self.default_role in self.roles

    def _system(self, role: str) -> str:
        if role == "chat":
            return build_system_prompt(
                self._cfg, "repository", self._library, self._chat_skills, subagent=True
            )
        return prompt_text(self._cfg.roles[role].prompt).rstrip() + "\n"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(False, "error: prompt is required")
        role = str(args.get("role") or self.default_role)
        if role not in self.roles:
            return ToolResult(
                False, f"error: unknown role '{role}'; the roles for a task are {', '.join(self.roles)}"
            )
        spec = self._cfg.roles[role]
        try:
            registry = cast(_Observed, _Observed.boxed(subagent_tools(self._cfg), ctx))
            registry.observer = OBSERVER.get()
            req = GenRequest(
                role=role,
                messages=[
                    Message(role="system", content=self._system(role)),
                    Message(role="user", content=prompt),
                ],
                sampling=spec.sampling,
                max_tokens=spec.max_tokens,
                thinking=spec.thinking,
            )
            engine = self._engine_for(role)
            try:
                res = await generate_with_tools(engine, req, registry, self._cfg.chat.task.max_steps)
            except asyncio.CancelledError:  # Stop: end the generation as the chat's own turn does
                with contextlib.suppress(Exception):
                    await engine.cancel(req.request_id)
                raise
        except (EngineError, ValueError) as e:  # a failed generation, or a ctx with no VM
            return ToolResult(False, f"error: the {role} subagent failed: {e}")
        answer = res.text.strip() or NO_ANSWER
        cap = ctx.output_cap_chars
        if len(answer) > cap:
            answer = answer[:cap] + "\n[the answer was cut]"
        return ToolResult(True, answer)
