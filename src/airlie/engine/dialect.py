"""The per-engine-kind request fields, in one place: how a role's thinking, its budget and its tool parser
are spelled for the server. HTTPEngine, the AutoGen client and serve all merge what this returns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

Dialect = Literal["mlx_vlm"]

LATE_KEYS = ("thinking_budget", "tool_parser")
"""Fields HTTPEngine._body sets after the tools."""


def dialect_for(kind: str) -> Dialect:
    """The dialect an engine kind speaks: mlx-vlm's, for every kind."""
    return "mlx_vlm"


def thinking_fields(
    *,
    dialect: Dialect,
    thinking: bool,
    budget: int | None,
    chat_template_kwargs: Mapping[str, object],
    tool_parser: str | None = None,
    tools: bool = False,
) -> dict[str, Any]:
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
