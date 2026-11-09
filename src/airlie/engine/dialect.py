"""The per-engine-kind request fields, in one place: how a role's thinking, its budget and its tool parser
are spelled for each server. HTTPEngine, the AutoGen client and serve all merge what this returns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

Dialect = Literal["vllm_mlx", "mlx_vlm"]

LATE_KEYS = ("thinking_token_budget", "thinking_budget", "tool_parser")
"""Fields HTTPEngine._body sets after the tools (where it set the budget before this module existed): kept
there so a vllm-mlx request body stays byte for byte what it was."""


def dialect_for(kind: str) -> Dialect:
    """The dialect an engine kind speaks. openai_http is vllm_mlx until phase 5."""
    return "mlx_vlm" if kind == "mlx_vlm" else "vllm_mlx"


def thinking_fields(
    *,
    dialect: Dialect,
    thinking: bool,
    budget: int | None,
    chat_template_kwargs: Mapping[str, object],
    tool_parser: str | None = None,
    tools: bool = False,
) -> dict[str, Any]:
    if dialect == "mlx_vlm":
        # mlx-vlm reads enable_thinking and (as reasoning_effort) the reasoning strength, no other template
        # argument; it names its budget thinking_budget and takes its tool parser per request
        out: dict[str, Any] = {"enable_thinking": bool(thinking)}
        if thinking and budget:
            out["thinking_budget"] = budget
        if "reasoning_strength" in chat_template_kwargs:
            out["reasoning_effort"] = chat_template_kwargs["reasoning_strength"]
        if tools and tool_parser:
            out["tool_parser"] = tool_parser
        return out
    # vllm-mlx gates its thinking-budget processor on the top-level flag (default True), not on the template
    # kwarg: without it a server started with a default budget cuts a non-thinking reply at the budget and
    # stamps </think> into it (AIR-7 layer 02, 2026-10-18); it drops a budget key it does not know
    out = {
        "chat_template_kwargs": {"enable_thinking": bool(thinking), **chat_template_kwargs},
        "enable_thinking": bool(thinking),
    }
    if thinking and budget:
        out["thinking_token_budget"] = budget
    return out
