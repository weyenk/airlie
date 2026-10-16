"""AutoGenTeam: the spec's AutoGen team orchestrator (01 §2) over the same seams as NativeTeam.

One `AssistantAgent` with `max_tool_iterations=1` is driven step by step so the StepObserver can inject
shared memory keys before each model call and receive each (u_t, o_t) afterwards. The model client is
AutoGen's OpenAI client pointed at the engine endpoint, so all generation still goes through vllm-mlx.
Thinking and top_k are server defaults (`--default-chat-template-kwargs`, `--default-top-k`) because
AutoGen's client has no per-request extra_body."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    TextMessage,
    ThoughtEvent,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
    ToolCallSummaryMessage,
)
from autogen_core import CancellationToken
from autogen_core.models import ModelInfo, UserMessage
from autogen_core.tools import BaseTool
from pydantic import BaseModel, create_model

from airlie.config import RoleSpec
from airlie.engine.types import ToolCall
from airlie.scheduler import SlotScheduler
from airlie.skills import prompt_text
from airlie.teams.autogen_client import RepairingOpenAIClient
from airlie.teams.native_team import _goal_message
from airlie.teams.protocol import StepObserver, StepRecord, StopReason, TeamContext, TeamResult, cut_off_text
from airlie.tools import ToolRegistry
from airlie.tools.protocol import Tool

_JSON_TO_PY: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class ToolError(Exception):
    """Raised by _RegistryTool when the airlie tool reports failure, so AutoGen marks the result is_error."""


class _RegistryTool(BaseTool[BaseModel, BaseModel]):
    """Exposes one airlie Tool to AutoGen; execution goes through the registry (sandbox, timeouts, caps)."""

    def __init__(
        self, tool: Tool, registry: ToolRegistry, scheduler: SlotScheduler | None = None, team_id: str = ""
    ) -> None:
        props = tool.parameters.get("properties", {})
        required = set(tool.parameters.get("required", []))
        fields: dict[str, Any] = {}
        for name, spec in props.items():
            typ = _JSON_TO_PY.get(str(spec.get("type", "string")), str)
            fields[name] = (typ, ...) if name in required else (typ | None, spec.get("default"))
        args_model = create_model(f"{tool.name}_args", **fields)
        out_model = create_model(f"{tool.name}_out", output=(str, ...))
        super().__init__(args_model, out_model, tool.name, tool.description)
        self._tool = tool
        self._registry = registry
        self._scheduler = scheduler
        self._team_id = team_id
        self._required = sorted(required)

    async def run_json(
        self, args: Mapping[str, Any], cancellation_token: CancellationToken, call_id: str | None = None
    ) -> Any:
        if "__truncated__" in args:  # repair_create_result could not decode the arguments
            tail = str(args["__truncated__"])[-40:]
            raise ToolError(
                f"error: the arguments of {self._tool.name} were cut off (the generation ran away, ending in "
                f"{tail!r}). Make a shorter call: write a smaller file or split the change."
            )
        missing = [n for n in self._required if args.get(n) is None]
        if missing:  # a clear error instead of pydantic's, so the model can correct the call
            raise ToolError(f"error: {self._tool.name} requires: {', '.join(missing)}")
        return await super().run_json(args, cancellation_token, call_id)

    async def run(self, args: BaseModel, cancellation_token: CancellationToken) -> BaseModel:
        payload = {k: v for k, v in args.model_dump().items() if v is not None}
        call = ToolCall(id="", name=self._tool.name, arguments=json.dumps(payload))
        if self._scheduler is not None:  # spec 04 §2: yield the generation slot while the tool runs
            async with self._scheduler.tool_stall(self._team_id):
                result = await self._registry.call(call)
        else:
            result = await self._registry.call(call)
        if not result.ok:  # AutoGen sets is_error only on exceptions; the fork trigger and LTS rely on it
            raise ToolError(result.output)
        out: BaseModel = self.return_type()(output=result.output)
        return out

    def return_value_as_string(self, value: BaseModel) -> str:
        return str(getattr(value, "output", value))


def build_function_tools(
    registry: ToolRegistry, scheduler: SlotScheduler | None = None, team_id: str = ""
) -> list[BaseTool[Any, Any] | Callable[..., Any]]:
    return [_RegistryTool(t, registry, scheduler, team_id) for t in registry._tools.values()]  # noqa: SLF001


def build_model_client(
    base_url: str,
    served_model: str,
    role: RoleSpec,
    api_key: str | None,
    scheduler: SlotScheduler | None = None,
    team_id: str = "",
) -> RepairingOpenAIClient:
    info: ModelInfo = {
        "vision": False,
        "function_calling": True,
        "json_output": False,
        "family": "unknown",
        "structured_output": False,
    }
    client = RepairingOpenAIClient(
        model=served_model,
        base_url=base_url,
        api_key=api_key or "airlie",
        model_info=info,
        temperature=role.sampling.temperature,
        top_p=role.sampling.top_p,
        max_tokens=role.max_tokens,
        extra_body={  # sampling the OpenAI client has no field for; vllm-mlx reads these per request
            "top_k": role.sampling.top_k,
            "min_p": role.sampling.min_p,
            "repetition_penalty": role.sampling.repetition_penalty,
        },  # type: ignore[call-arg]  # accepted via create_kwargs ("extra_body"), not in the typed config
    )
    client.slot_scheduler = scheduler
    client.team_id = team_id
    return client


def pair_steps(
    team_id: str, step_offset: int, events: Sequence[BaseAgentEvent | BaseChatMessage], *, assistant_text: str
) -> list[StepRecord]:
    """Match ToolCallRequestEvent calls with ToolCallExecutionEvent results into StepRecords, in order."""
    results: dict[str, tuple[str, bool]] = {}
    for ev in events:
        if isinstance(ev, ToolCallExecutionEvent):
            for r in ev.content:
                results[r.call_id] = (str(r.content), not bool(r.is_error))
    steps: list[StepRecord] = []
    n = step_offset
    for ev in events:
        if isinstance(ev, ToolCallRequestEvent):
            for call in ev.content:
                n += 1
                out, ok = results.get(call.id, ("(no result recorded for this call)", False))
                steps.append(
                    StepRecord(
                        team_id=team_id,
                        step_num=n,
                        u_t=ToolCall(id=call.id, name=call.name, arguments=call.arguments),
                        o_t=out,
                        ok=ok,
                        assistant_text=assistant_text,
                    )
                )
    return steps


async def agent_state(agent: AssistantAgent) -> dict[str, Any]:
    return dict(await agent.save_state())


async def load_agent_state(agent: AssistantAgent, state: dict[str, Any]) -> None:
    await agent.load_state(state)


class AutoGenTeam:
    def __init__(self, base_url: str, served_model: str, api_key: str | None = None) -> None:
        self.base_url = base_url
        self.served_model = served_model
        self.api_key = api_key

    async def run(self, ctx: TeamContext, observer: StepObserver) -> TeamResult:
        t0 = time.monotonic()
        client = build_model_client(
            self.base_url,
            self.served_model,
            ctx.role,
            self.api_key,
            scheduler=ctx.scheduler,
            team_id=ctx.team_id,
        )
        agent = AssistantAgent(
            name=ctx.team_id.replace("-", "_"),
            model_client=client,
            tools=build_function_tools(ctx.tools, ctx.scheduler, ctx.team_id),
            system_message=prompt_text(ctx.role.prompt),
            reflect_on_tool_use=False,
            max_tool_iterations=1,
        )
        steps = 0
        prompt_tokens = completion_tokens = 0
        max_prompt = 0
        recent: list[tuple[str, str, str]] = []
        goal_msg = _goal_message(ctx.goal, ctx.worktree.name, ctx.team_id, ctx.preamble)
        task: str | None = goal_msg
        if ctx.initial_state is not None:
            await load_agent_state(agent, ctx.initial_state)
            task = ctx.nudge or goal_msg
        last_test_output = ""
        forked = False
        state: dict[str, Any] | None = None
        final_text = ""
        stop: StopReason = "done"
        ok = False
        try:
            while True:
                injected = await observer.before_step(ctx.team_id, steps + 1)
                if injected:
                    await agent.model_context.add_message(UserMessage(content=injected, source="airlie"))
                events: list[BaseAgentEvent | BaseChatMessage] = []
                result: TaskResult | None = None
                async for item in agent.run_stream(task=task, output_task_messages=False):
                    if isinstance(item, TaskResult):
                        result = item
                    else:
                        events.append(item)
                task = None
                for ev in events:
                    usage = getattr(ev, "models_usage", None)
                    if usage:
                        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
                        prompt_tokens += pt
                        max_prompt = max(max_prompt, pt)
                        completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
                new_steps = pair_steps(ctx.team_id, steps, events, assistant_text="")
                for s in new_steps:
                    await observer.after_step(s)
                    if s.u_t.name == "run_tests":
                        last_test_output = s.o_t
                    recent.append((s.u_t.name, s.u_t.arguments, s.o_t))
                    if ctx.stop_when is not None and ctx.stop_when(s):
                        forked = True
                        break
                recent = recent[-ctx.repeat_limit :]
                steps += len(new_steps)
                if forked:
                    final_text = "fork point: stuck, handing over to branches"
                    stop = "fork_point"
                    break
                last = result.messages[-1] if result and result.messages else None
                if not new_steps:
                    final_text = (
                        last.content
                        if isinstance(last, TextMessage)
                        else (str(getattr(last, "content", "")) if last else "")
                    )
                    if client.last_finish_reason == "length":  # as NativeTeam: not a finished answer
                        if not final_text.strip():
                            thinking = any(isinstance(e, ThoughtEvent) and e.content.strip() for e in events)
                            final_text = cut_off_text(ctx.role_name, ctx.role, thinking=thinking)
                        stop = "cut_off"
                        break
                    ok = True
                    stop = "done"
                    break
                if isinstance(last, ToolCallSummaryMessage) and not new_steps:
                    break
                if len(recent) == ctx.repeat_limit and len(set(recent)) == 1:
                    final_text = (
                        f"stopped: repeated the same action {ctx.repeat_limit} times ({recent[0][0]})"
                    )
                    stop = "repeated_action"
                    break
                if steps >= ctx.max_steps:
                    final_text = f"stopped: reached max_steps={ctx.max_steps}"
                    stop = "max_steps"
                    break
                if max_prompt + completion_tokens >= ctx.token_budget:
                    final_text = f"stopped: token budget {ctx.token_budget} exhausted"
                    stop = "token_budget"
                    break
        except Exception as e:  # noqa: BLE001 - surface engine/client failures as a result, not a crash
            final_text = f"engine error: {type(e).__name__}: {e}"
            stop = "engine_error"
        finally:
            if ctx.stop_when is not None:  # every stop is a possible fork point (a quitter's "done" too)
                state = await agent_state(agent)
            await client.close()
        return TeamResult(
            team_id=ctx.team_id,
            ok=ok,
            stop_reason=stop,
            final_text=final_text,
            steps=steps,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            wall_ms=(time.monotonic() - t0) * 1000.0,
            state=state,
            last_test_output=last_test_output,
        )
