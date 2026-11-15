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

_QWEN_TOOL_RESPONSE_PREFIX = "<tool_response>"
_QWEN_TOOL_RESPONSE_SUFFIX = "</tool_response>"


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


def _user_message_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("text", "input_text", "output_text"):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return ""


def _is_qwen_tool_response_user(text: str) -> bool:
    return text.startswith(_QWEN_TOOL_RESPONSE_PREFIX) and text.endswith(_QWEN_TOOL_RESPONSE_SUFFIX)


def mlx_vlm_needs_user_query(messages: list[dict[str, Any]]) -> bool:
    """True when Qwen3's chat template would raise 'No user query found in messages.'."""
    for message in messages:
        if message.get("role") != "user":
            continue
        text = _user_message_text(message.get("content"))
        if text and not _is_qwen_tool_response_user(text):
            return False
    return True


def normalize_mlx_vlm_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep Qwen3 tool-loop histories valid for mlx-vlm generation and compaction renders."""
    if not messages:
        return messages
    out = [dict(m) for m in messages if isinstance(m, dict)]
    if not out:
        return out
    last = out[-1]
    role = last.get("role")
    needs_follow_up = role == "tool" or (role == "assistant" and last.get("tool_calls"))
    if needs_follow_up or mlx_vlm_needs_user_query(out):
        out.append({"role": "user", "content": MLX_VLM_TOOL_TURN_USER})
    return out
