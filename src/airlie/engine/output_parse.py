"""parse_output: apply a model's tool/reasoning parsers (vllm-mlx's own classes) to a GenResult.

Precedence: tool calls the server already parsed are kept; reasoning the server already split is kept;
otherwise the model's `reasoning_parser` runs on `text`, then its `tool_call_parser` on what is left."""

from __future__ import annotations

import json
import uuid
from functools import cache
from typing import Any

from airlie.config import ModelSpec
from airlie.engine.parsers import ensure_registered
from airlie.engine.types import GenResult, ToolCall


@cache
def _tool_parser(name: str) -> Any:
    ensure_registered()
    from vllm_mlx.tool_parsers import ToolParserManager

    return ToolParserManager.get_tool_parser(name)()


@cache
def _reasoning_parser(name: str) -> Any:
    ensure_registered()
    from vllm_mlx.reasoning import get_parser

    return get_parser(name)()


def _arguments(raw: Any) -> str:
    return raw if isinstance(raw, str) else json.dumps(raw)


def parse_output(model: ModelSpec, result: GenResult) -> GenResult:
    text = result.text
    if model.reasoning_parser and not result.reasoning and text:
        reasoning, content = _reasoning_parser(model.reasoning_parser).extract_reasoning(text)
        if reasoning:
            result.reasoning = reasoning
        if reasoning or (content is not None and content != text):  # markers present: take the parsed content
            text = content or ""
    if model.tool_call_parser and not result.tool_calls and text:
        parser = _tool_parser(model.tool_call_parser)
        parser.reset()
        info = parser.extract_tool_calls(text, None)
        if info.tools_called:
            result.tool_calls = [
                ToolCall(
                    id=str(c.get("id") or f"call_{uuid.uuid4().hex[:8]}"),
                    name=str(c["name"]),
                    arguments=_arguments(c.get("arguments", "{}")),
                )
                for c in info.tool_calls
            ]
            text = (info.content or "").strip()
            if result.finish_reason != "cancelled":
                result.finish_reason = "tool_calls"
    result.text = text
    return result
