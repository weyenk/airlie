"""The Muse call: one bounded run of the supervisor role on a triage message, with read-only tools and
`run_test`, that comes back as a parsed answer or as a TriageFailed that says why not.

Every way it can go wrong is a reason, never a stop: the caller falls back to today's message on any of them,
so the run goes on whatever Muse does."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from airlie.config import RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import GenRequest, Message
from airlie.skills import prompt_text
from airlie.supervisor.answer import Answer, AnswerError, parse_answer
from airlie.supervisor.evidence import TriageInput, triage_message
from airlie.tools.loop import DEFAULT_STEPS, generate_with_tools
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry


class OneTestRunner(Protocol):
    """What `run_test` needs to run a test: `PytestChecks` is one."""

    async def run_one(self, worktree: Path, test: str) -> str: ...


class RunOneTest:
    """Muse's one way to run anything: one pytest node id, through the gate's own pytest step (confined as
    the suite is), so it can see a failure for itself. It cannot run a command or change a file."""

    name = "run_test"
    description = (
        "Run one pytest test by node id and read the end of its output, to see why it fails. One test only; "
        "it changes nothing."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "test": {"type": "string", "description": "a pytest node id, e.g. tests/test_x.py::test_y"}
        },
        "required": ["test"],
    }

    def __init__(self, checks: OneTestRunner) -> None:
        self.checks = checks

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        test = str(args.get("test") or "").strip()
        if not test:
            return ToolResult(False, "error: give one pytest node id")
        return ToolResult(True, await self.checks.run_one(ctx.worktree, test))


class TriageFailed(Exception):
    """Muse gave no usable answer. `reason` is one of engine_error, timeout, unparsable, cut_off."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class Triaged:
    answer: Answer
    seconds: float
    tokens: int


class Triage:
    def __init__(
        self,
        engine: Engine,
        role_name: str,
        role: RoleSpec,
        tools: Callable[[Path], ToolRegistry],
        timeout_s: float,
    ) -> None:
        """`tools`: the registry for a worktree (read_file, list_dir, grep and run_test), built per call
        because the tools are confined to the worktree they are given."""
        self.engine = engine
        self.role_name = role_name
        self.role = role
        self.tools = tools
        self.timeout_s = timeout_s
        self.model = role.model
        """The role's model name, for the ledger."""

    async def __call__(self, inp: TriageInput, worktree: Path) -> Triaged:
        # The request is built inside the guard too: a missing prompt file or a broken tools factory is a
        # fallback like any other failure, never a stopped run.
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            # chat_template_kwargs is not a GenRequest field: the engine reads it from the role's spec by
            # name, as it does for the reviewer.
            req = GenRequest(
                role=self.role_name,
                messages=[
                    Message(role="system", content=prompt_text(self.role.prompt)),
                    Message(role="user", content=triage_message(inp)),
                ],
                sampling=self.role.sampling,
                max_tokens=self.role.max_tokens,
                thinking=self.role.thinking,
            )
            res = await asyncio.wait_for(
                generate_with_tools(
                    self.engine, req, self.tools(worktree), self.role.max_steps or DEFAULT_STEPS
                ),
                self.timeout_s,
            )
        except TriageFailed:
            raise
        except TimeoutError as e:
            raise TriageFailed("timeout", f"no answer in {self.timeout_s:g}s") from e
        except Exception as e:  # noqa: BLE001  # whatever broke, the run goes on without Muse; cancellation is not an Exception
            raise TriageFailed("engine_error", f"{type(e).__name__}: {e}") from e
        if res.finish_reason == "length" and not res.text.strip():
            raise TriageFailed("cut_off", "max_tokens ended the reply before an answer")
        try:
            answer = parse_answer(res.text, block=inp.trigger == "block")
        except AnswerError as e:
            raise TriageFailed("unparsable", str(e)) from e
        return Triaged(answer, loop.time() - started, res.prompt_tokens + res.completion_tokens)
