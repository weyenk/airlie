"""AutoGen model client wrapper: repairs tool-call arguments and drops duplicates before AutoGen parses them.

vllm-mlx 0.5.0 emits each tool call twice on the stream (same index, two ids; see tests/fixtures/vllm_mlx) and
its qwen3_coder parser can append a stray brace to JSON arguments. AutoGen executes whatever it receives, so
both are fixed at the client boundary."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

from autogen_core import CancellationToken, FunctionCall
from autogen_core.models import CreateResult, LLMMessage
from autogen_core.tools import Tool, ToolSchema
from autogen_ext.models.openai import OpenAIChatCompletionClient

from airlie.scheduler import SlotScheduler
from airlie.tools.jsonargs import repair_json_args


def repair_create_result(result: CreateResult) -> CreateResult:
    if not isinstance(result.content, list):
        return result
    fixed: list[FunctionCall] = []
    for call in result.content[:1]:  # one action per step: later calls in a "plan" are often argument-less
        args = repair_json_args(call.arguments)
        try:
            json.loads(args)
        except (
            json.JSONDecodeError
        ):  # cut off mid-string (runaway generation): tell the model, do not execute
            args = json.dumps({"__truncated__": call.arguments[-200:]})
        fixed.append(FunctionCall(id=call.id, name=call.name, arguments=args))
    result.content = fixed
    return result


class RepairingOpenAIClient(OpenAIChatCompletionClient):
    """Also takes a generation slot from the SlotScheduler for each model call (spec 04 §2)."""

    slot_scheduler: SlotScheduler | None = None
    team_id: str = ""

    def _slot(self) -> AbstractAsyncContextManager[None]:
        if self.slot_scheduler is None:
            return nullcontext()
        return self.slot_scheduler.generation(self.team_id)

    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],  # noqa: B006 - mirrors the base signature
        tool_choice: Any = "auto",
        json_output: Any = None,
        extra_create_args: Any = None,
        cancellation_token: CancellationToken | None = None,
    ) -> CreateResult:
        async with self._slot():
            res = await super().create(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=extra_create_args or {},
                cancellation_token=cancellation_token,
            )
        return repair_create_result(res)

    async def create_stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],  # noqa: B006
        tool_choice: Any = "auto",
        json_output: Any = None,
        extra_create_args: Any = None,
        cancellation_token: CancellationToken | None = None,
        max_consecutive_empty_chunk_tolerance: int = 0,
        include_usage: bool | None = None,
    ) -> AsyncGenerator[str | CreateResult, None]:
        async with self._slot():
            async for item in super().create_stream(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=extra_create_args or {},
                cancellation_token=cancellation_token,
                max_consecutive_empty_chunk_tolerance=max_consecutive_empty_chunk_tolerance,
                include_usage=include_usage,
            ):
                yield repair_create_result(item) if isinstance(item, CreateResult) else item
