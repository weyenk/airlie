"""AutoGenTeam: the spec's AutoGen team orchestrator (01 §2) over the same seams as NativeTeam.

One `AssistantAgent` with `max_tool_iterations=1` is driven step by step so the StepObserver can inject
shared memory keys before each model call and receive each (u_t, o_t) afterwards. The model client is
AutoGen's OpenAI client pointed at the engine endpoint, so all generation still goes through vllm-mlx.
Thinking and top_k are server defaults (`--default-chat-template-kwargs`, `--default-top-k`) because
AutoGen's client has no per-request extra_body."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Any

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
    ToolCallSummaryMessage,
)
from autogen_core import CancellationToken
from autogen_core.models import ModelInfo, UserMessage
from autogen_core.tools import BaseTool
from autogen_ext.models.openai import OpenAIChatCompletionClient
from pydantic import BaseModel, create_model

from airlie.config import RoleSpec
from airlie.engine.types import ToolCall
from airlie.teams.autogen_client import RepairingOpenAIClient
from airlie.teams.native_team import _goal_message
from airlie.teams.protocol import StepObserver, StepRecord, StopReason, TeamContext, TeamResult
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


class _RegistryTool(BaseTool[BaseModel, BaseModel]):
    """Exposes one airlie Tool to AutoGen; execution goes through the registry (sandbox, timeouts, caps)."""

    def __init__(self, tool: Tool, registry: ToolRegistry) -> None:
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

    async def run(self, args: BaseModel, cancellation_token: CancellationToken) -> BaseModel:
        payload = {k: v for k, v in args.model_dump().items() if v is not None}
        result = await self._registry.call(
            ToolCall(id="", name=self._tool.name, arguments=json.dumps(payload))
        )
        out: BaseModel = self.return_type()(output=result.output)
        return out

    def return_value_as_string(self, value: BaseModel) -> str:
        return str(getattr(value, "output", value))


def build_function_tools(registry: ToolRegistry) -> list[BaseTool[Any, Any] | Callable[..., Any]]:
    return [_RegistryTool(t, registry) for t in registry._tools.values()]  # noqa: SLF001


def build_model_client(
    base_url: str, served_model: str, role: RoleSpec, api_key: str | None
) -> OpenAIChatCompletionClient:
    info: ModelInfo = {
        "vision": False,
        "function_calling": True,
        "json_output": False,
        "family": "unknown",
        "structured_output": False,
    }
    return RepairingOpenAIClient(
        model=served_model,
        base_url=base_url,
        api_key=api_key or "airlie",
        model_info=info,
        temperature=role.sampling.temperature,
        top_p=role.sampling.top_p,
        max_tokens=role.max_tokens,
    )


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


class AutoGenTeam:
    def __init__(self, base_url: str, served_model: str, api_key: str | None = None) -> None:
        self.base_url = base_url
        self.served_model = served_model
        self.api_key = api_key

    async def run(self, ctx: TeamContext, observer: StepObserver) -> TeamResult:
        t0 = time.monotonic()
        client = build_model_client(self.base_url, self.served_model, ctx.role, self.api_key)
        agent = AssistantAgent(
            name=ctx.team_id.replace("-", "_"),
            model_client=client,
            tools=build_function_tools(ctx.tools),
            system_message=ctx.role.prompt.read_text(),
            reflect_on_tool_use=False,
            max_tool_iterations=1,
        )
        steps = 0
        prompt_tokens = completion_tokens = 0
        recent: list[tuple[str, str]] = []
        task: str | None = _goal_message(ctx.goal, ctx.worktree.name)
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
                async with ctx.scheduler.generation(ctx.team_id):
                    async for item in agent.run_stream(task=task, output_task_messages=False):
                        if isinstance(item, TaskResult):
                            result = item
                        else:
                            events.append(item)
                task = None
                for ev in events:
                    usage = getattr(ev, "models_usage", None)
                    if usage:
                        prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                        completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
                new_steps = pair_steps(ctx.team_id, steps, events, assistant_text="")
                for s in new_steps:
                    await observer.after_step(s)
                    recent.append((s.u_t.name, s.u_t.arguments))
                recent = recent[-ctx.repeat_limit :]
                steps += len(new_steps)
                last = result.messages[-1] if result and result.messages else None
                if not new_steps:
                    final_text = (
                        last.content
                        if isinstance(last, TextMessage)
                        else (str(getattr(last, "content", "")) if last else "")
                    )
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
                if prompt_tokens + completion_tokens >= ctx.token_budget:
                    final_text = f"stopped: token budget {ctx.token_budget} exhausted"
                    stop = "token_budget"
                    break
        except Exception as e:  # noqa: BLE001 - surface engine/client failures as a result, not a crash
            final_text = f"engine error: {type(e).__name__}: {e}"
            stop = "engine_error"
        finally:
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
        )
