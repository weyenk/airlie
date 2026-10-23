"""The Muse call: one bounded run of the supervisor role on a triage message, with read-only tools and
`run_test`, that comes back as a parsed answer or as a TriageFailed that says why not.

Every way it can go wrong is a reason, never a stop: the caller falls back to today's message on any of them,
so the run goes on whatever Muse does."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from airlie.config import AirlieConfig, RoleSpec
from airlie.engine.protocol import Engine, OnToken
from airlie.engine.types import GenRequest, GenResult, Message
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
    """Muse gave no usable answer. `reason` is one of engine_error, timeout, unparsable, cut_off; `seconds`
    and `tokens` are what the attempt took all the same, for the ledger."""

    def __init__(self, reason: str, detail: str = "", *, seconds: float = 0.0, tokens: int = 0) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.seconds = seconds
        self.tokens = tokens


class _Counted:
    """The engine, adding up the tokens of every request made through it: a triage is a tool loop of several
    requests, and its cost is all of them, not the last."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.tokens = 0

    async def generate(self, req: GenRequest, on_token: OnToken | None = None) -> GenResult:
        res = await self.engine.generate(req, on_token)
        self.tokens += res.prompt_tokens + res.completion_tokens
        return res

    def __getattr__(self, name: str) -> Any:
        return getattr(self.engine, name)


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
        *,
        model: str | None = None,
    ) -> None:
        """`tools`: the registry for a worktree (read_file, list_dir, grep and run_test), built per call
        because the tools are confined to the worktree they are given. `model`: what the ledger records as
        the model Muse ran on (the weights, from the catalog); the role's seat name without one."""
        self.engine = engine
        self.role_name = role_name
        self.role = role
        self.tools = tools
        self.timeout_s = timeout_s
        self.model = model or role.model
        """The model for the ledger: a seat (`reviewer`) can be pointed at other weights between rows."""

    async def __call__(
        self, inp: TriageInput, worktree: Path, *, on_steer: Callable[[str], None] | None = None
    ) -> Triaged:
        # The request is built inside the guard too: a missing prompt file or a broken tools factory is a
        # fallback like any other failure, never a stopped run. `on_steer` hears each hub steering text the
        # lookup loop sends Muse, for a caller with an event stream (`airlie supervisor try` has none).
        loop = asyncio.get_running_loop()
        started = loop.time()
        engine = _Counted(self.engine)

        def failed(reason: str, detail: str) -> TriageFailed:
            return TriageFailed(reason, detail, seconds=loop.time() - started, tokens=engine.tokens)

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
                    cast(Engine, engine),
                    req,
                    self.tools(worktree),
                    self.role.max_steps or DEFAULT_STEPS,
                    on_steer=on_steer,
                ),
                self.timeout_s,
            )
        except TimeoutError as e:
            raise failed("timeout", f"no answer in {self.timeout_s:g}s") from e
        except Exception as e:  # noqa: BLE001  # whatever broke, the run goes on without Muse; cancellation is not an Exception
            raise failed("engine_error", f"{type(e).__name__}: {e}") from e
        if res.finish_reason == "length" and not res.text.strip():
            raise failed("cut_off", "max_tokens ended the reply before an answer")
        try:
            answer = parse_answer(res.text, block=inp.trigger == "block")
        except AnswerError as e:
            raise failed("unparsable", str(e)) from e
        return Triaged(answer, loop.time() - started, engine.tokens)


def triage_for(cfg: AirlieConfig, engine: Engine, checks: OneTestRunner | None) -> Triage:
    """story.supervisor's Triage on `engine`: its role, with read_file, list_dir and grep confined to the
    worktree each call is given, and run_test when the gate runs pytest (`checks`). The pipeline and
    `airlie supervisor try` both build it here, so a try asks Muse exactly as a story does."""
    spec = cfg.story.supervisor
    tools_cfg = cfg.tools

    def tools(worktree: Path) -> ToolRegistry:
        ctx = ToolContext(
            worktree, tools_cfg.shell_timeout_s, tools_cfg.output_cap_chars, **tools_cfg.confinement()
        )
        registry = ToolRegistry.builtin(["read_file", "list_dir", "grep"], ctx)
        if checks is not None:
            registry.add(RunOneTest(checks))
        return registry

    role = cfg.roles[spec.role]
    weights = cfg.models.get(role.model)
    return Triage(
        engine, spec.role, role, tools, spec.timeout_s, model=weights.repo if weights is not None else None
    )
