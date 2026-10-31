"""AutoGen model client wrapper: repairs tool-call arguments and drops duplicates before AutoGen parses them.

vllm-mlx 0.5.0 emits each tool call twice on the stream (same index, two ids; see tests/fixtures/vllm_mlx) and
its qwen3_coder parser can append a stray brace to JSON arguments. AutoGen executes whatever it receives, so
both are fixed at the client boundary.

The server runs one tool parser for every model it serves, so a model with its own format (Muse) comes back as
text; with its `model_spec` set, the client applies the model's parsers as HTTPEngine does (`parse_output`),
and a call on a request that offered no tools (AutoGen's reflection) is asked again once for the answer."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

from autogen_core import CancellationToken, FunctionCall
from autogen_core.models import (
    AssistantMessage,
    CreateResult,
    FunctionExecutionResultMessage,
    LLMMessage,
    UserMessage,
)
from autogen_core.tools import Tool, ToolSchema
from autogen_ext.models.openai import OpenAIChatCompletionClient

from airlie.config import ModelSpec
from airlie.engine.output_parse import parse_output
from airlie.engine.types import GenResult
from airlie.prompts import entry
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


_BARE_HEADERS = re.compile(r"\bto=\w+")
"""A channel header with nothing after it (Muse's bare `to=user`, run 6, 2026-10-26) is no answer."""


def parsed_text(model: ModelSpec, result: CreateResult) -> GenResult | None:
    """The model's parsers over a reply the server left as text; None when it is already calls."""
    if not isinstance(result.content, str):
        return None
    gen = GenResult(
        text=result.content,
        reasoning=result.thought or "",
        tool_calls=[],
        finish_reason="stop",
        prompt_tokens=0,
        completion_tokens=0,
    )
    return parse_output(model, gen)


class RepairingOpenAIClient(OpenAIChatCompletionClient):
    """Also takes a generation slot from the SlotScheduler for each model call (spec 04 §2), and remembers how
    the latest reply ended: AutoGen's agent does not pass that on."""

    slot_scheduler: SlotScheduler | None = None
    team_id: str = ""
    last_finish_reason: str = ""
    """How the latest reply ended; "length" means max_tokens cut it off."""
    model_spec: ModelSpec | None = None
    """The served model's catalog entry, whose parsers read a reply the server left as text."""
    reflection_note: str = ""
    """Said after the no-tools note on AutoGen's reflection request: what the caller's answer must hold (the
    meeting's verdict). Empty: nothing more."""

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
        async def ask(history: Sequence[LLMMessage]) -> CreateResult:
            async with self._slot():
                return await super(RepairingOpenAIClient, self).create(
                    history,
                    tools=tools,
                    tool_choice=tool_choice,
                    json_output=json_output,
                    extra_create_args=extra_create_args or {},
                    cancellation_token=cancellation_token,
                )

        if not tools and messages and isinstance(messages[-1], FunctionExecutionResultMessage):
            # AutoGen's reflection after a tool round offers no tools: say so up front, or a role that still
            # wants to read answers "let me read ..." in prose, with no verdict (wiki run B, 2026-10-27)
            note = entry("last-turn-retry").body.strip()
            messages = [
                *messages,
                UserMessage(content=f"{note}\n\n{self.reflection_note}".strip(), source="airlie"),
            ]
        res = await ask(messages)
        if self.model_spec is not None:
            res = await self._parsed(res, messages, offered=bool(tools), ask=ask)
        self.last_finish_reason = res.finish_reason
        return repair_create_result(res)

    async def _parsed(
        self, res: CreateResult, messages: Sequence[LLMMessage], *, offered: bool, ask: Any
    ) -> Any:
        assert self.model_spec is not None
        gen = parsed_text(self.model_spec, res)
        if gen is None:
            return res
        empty = not gen.tool_calls and not _BARE_HEADERS.sub("", gen.text).strip()
        if (gen.tool_calls and not offered) or empty:  # nothing it calls can run, or no answer: once, ask
            retry = entry("empty-reply" if empty else "last-turn-retry").body.strip()
            again = await ask(
                [
                    *messages,
                    AssistantMessage(content=res.content, source="assistant"),
                    UserMessage(content=retry, source="airlie"),
                ]
            )
            gen = parsed_text(self.model_spec, again) or gen
            res = again
        res.thought = gen.reasoning or res.thought
        if gen.tool_calls and offered:
            res.content = [FunctionCall(id=c.id, name=c.name, arguments=c.arguments) for c in gen.tool_calls]
            res.finish_reason = "function_calls"
        else:
            res.content = gen.text if _BARE_HEADERS.sub("", gen.text).strip() else ""
        return res

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
                if isinstance(item, CreateResult):
                    self.last_finish_reason = item.finish_reason
                    yield repair_create_result(item)
                else:
                    yield item
