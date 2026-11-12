"""What each chat kind may use: the per-kind tool registry. A plain chat has no repository, so it holds only
the web tools, `skill` and `todo`; the repository kind comes with plan 4."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.tools import SkillTool, TodoList, TodoTool
from airlie.config.schema import CHAT_TOOLS, PLAIN_CHAT_FORBIDDEN, AirlieConfig
from airlie.tools.protocol import Tool, ToolContext
from airlie.tools.registry import ToolRegistry

ChatKind = Literal["plain", "repository"]
PLAIN_TOOLS: frozenset[str] = CHAT_TOOLS - PLAIN_CHAT_FORBIDDEN
REPOSITORY_TOOLS: frozenset[str] = CHAT_TOOLS
KNOWN_CHAT_TOOLS: frozenset[str] = CHAT_TOOLS


class KindUnavailable(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(f"the {kind} chat kind is not available yet")
        self.kind = kind


def ask_first(kind: ChatKind, cfg: AirlieConfig) -> frozenset[str]:
    spec = cfg.chat.kinds.plain if kind == "plain" else cfg.chat.kinds.repository
    return frozenset(spec.ask_first)


def registry_for(
    kind: ChatKind,
    cfg: AirlieConfig,
    *,
    library: SuperpowersLibrary | None,
    todos: TodoList,
    ctx: ToolContext,
    web: Mapping[str, Tool],
) -> ToolRegistry:
    if kind != "plain":
        raise KindUnavailable(kind)
    tools: list[Tool] = []
    for name in cfg.chat.kinds.plain.tools:
        if name in ("web_search", "web_fetch"):
            if name in web:
                tools.append(web[name])
        elif name == "skill":
            if cfg.chat.superpowers.enabled and library is not None:
                tools.append(SkillTool(library))
        elif name == "todo":
            tools.append(TodoTool(todos))
    return ToolRegistry(tools, ctx)
