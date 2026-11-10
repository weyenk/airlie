"""The per-engine-kind request fields, in one place: how a role's thinking, its budget and its tool parser
are spelled for the server. HTTPEngine, the AutoGen client and serve all merge what this returns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

Dialect = Literal["mlx_vlm", "openai"]

LATE_KEYS = ("thinking_budget", "tool_parser")
"""Fields HTTPEngine._body sets after the tools."""

# OpenCode and other OpenAI clients use Chat Completions names mlx-vlm does not accept on its own.
MLX_VLM_REASONING_EFFORT: dict[str, str] = {
    "high": "xhigh",
    "minimal": "low",
    "none": "low",
}
"""Map client reasoning labels to mlx-vlm's xhigh, medium, and low."""

MLX_VLM_TOOL_TURN_USER = "Continue."
"""User turn appended after tool results when the client omits the next user message (OpenCode)."""


def dialect_for(kind: str) -> Dialect:
    """The dialect an engine kind speaks: mlx-vlm's, or plain OpenAI-compatible for an external server."""
    return "mlx_vlm" if kind == "mlx_vlm" else "openai"


def thinking_fields(
    *,
    dialect: Dialect,
    thinking: bool,
    budget: int | None,
    chat_template_kwargs: Mapping[str, object],
    tool_parser: str | None = None,
    tools: bool = False,
) -> dict[str, Any]:
    if dialect == "openai":  # an external server: only the template kwargs a generic one may read
        return {"chat_template_kwargs": {"enable_thinking": bool(thinking), **chat_template_kwargs}}
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


def remap_mlx_vlm_reasoning_effort(body: dict[str, Any]) -> None:
    """Rewrite client reasoning labels in place so mlx-vlm's chat template accepts them."""
    effort = body.get("reasoning_effort")
    if isinstance(effort, str):
        mapped = MLX_VLM_REASONING_EFFORT.get(effort, effort)
        if mapped != effort:
            body["reasoning_effort"] = mapped
    kwargs = body.get("chat_template_kwargs")
    if isinstance(kwargs, dict):
        strength = kwargs.get("reasoning_strength")
        if isinstance(strength, str):
            mapped = MLX_VLM_REASONING_EFFORT.get(strength, strength)
            if mapped != strength:
                body["chat_template_kwargs"] = {**kwargs, "reasoning_strength": mapped}


def normalize_mlx_vlm_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Qwen3's template errors when a tool-loop turn has no user message; OpenCode often omits that turn."""
    if not messages:
        return messages
    out = [dict(m) for m in messages if isinstance(m, dict)]
    if out and out[-1].get("role") == "tool":
        out.append({"role": "user", "content": MLX_VLM_TOOL_TURN_USER})
    return out
