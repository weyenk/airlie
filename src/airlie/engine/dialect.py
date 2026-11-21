"""The per-engine-format request fields, in one place: how a role's thinking is spelled for the server.
An engine's ``format`` is its dialect: ``openai`` (Chat Completions) or ``anthropic`` (Messages, translated
by ``airlie.engine.anthropic``). HTTPEngine, the AutoGen client and serve all merge what this returns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

Dialect = Literal["openai", "anthropic"]

LATE_KEYS = ("thinking_budget", "tool_parser")
"""Fields HTTPEngine._body sets after the tools."""


def dialect_for(format: str) -> Dialect:  # noqa: A002  (the engine field's own name)
    """The dialect an engine's ``format`` names; anything else is an error."""
    if format not in ("openai", "anthropic"):
        raise ValueError(f"unknown engine format {format!r}: expected 'openai' or 'anthropic'")
    return cast(Dialect, format)


def thinking_fields(
    *,
    dialect: Dialect,
    thinking: bool,
    budget: int | None,
    chat_template_kwargs: Mapping[str, object],
    tool_parser: str | None = None,
    tools: bool = False,
) -> dict[str, Any]:
    """Only the template kwargs a generic OpenAI-compatible server may read. ``budget``, ``tool_parser`` and
    ``tools`` are accepted for the callers' sake and unused."""
    return {"chat_template_kwargs": {"enable_thinking": bool(thinking), **chat_template_kwargs}}
