"""What each chat kind may use: the per-kind tool registry. A plain chat has no repository, so it holds only
the web tools, `skill` and `todo`; a repository chat adds the tools of its VM (chat.repo_tools)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal

from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.tools import SkillTool, TodoList, TodoTool
from airlie.config.schema import ALWAYS_ASK, CHAT_TOOLS, PLAIN_CHAT_FORBIDDEN, AirlieConfig
from airlie.engine.protocol import Engine
from airlie.skills.library import Skill
from airlie.tools.protocol import Tool, ToolContext
from airlie.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from airlie.chat.repo_tools import RepoChat

ChatKind = Literal["plain", "repository"]
PLAIN_TOOLS: frozenset[str] = CHAT_TOOLS - PLAIN_CHAT_FORBIDDEN
REPOSITORY_TOOLS: frozenset[str] = CHAT_TOOLS
KNOWN_CHAT_TOOLS: frozenset[str] = CHAT_TOOLS


class KindUnavailable(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(f"the {kind} chat kind needs a repository session to build its tools")
        self.kind = kind


def ask_first(kind: ChatKind, cfg: AirlieConfig) -> frozenset[str]:
    spec = cfg.chat.kinds.plain if kind == "plain" else cfg.chat.kinds.repository
    return frozenset(spec.ask_first)


def needs_yes(kind: ChatKind, cfg: AirlieConfig) -> frozenset[str]:
    """What `kind` never runs without a person's yes: its `ask_first`, and for a repository chat the
    always-asked tools whatever the config lists. The guard and the subagent's tool list both read this."""
    listed = ask_first(kind, cfg)
    return listed | ALWAYS_ASK if kind == "repository" else listed


def registry_for(
    kind: ChatKind,
    cfg: AirlieConfig,
    *,
    library: SuperpowersLibrary | None,
    todos: TodoList,
    ctx: ToolContext,
    web: Mapping[str, Tool],
    skills_extra: Mapping[str, Skill] | None = None,
    repo: RepoChat | None = None,
    task_engine_for: Callable[[str], Engine] | None = None,
) -> ToolRegistry:
    """The tools of `kind`. A repository chat needs its opened session (`repo`): its file, shell and git tools
    run in that session's VM, and `ctx` is that VM's context."""
    if kind == "repository":
        if repo is None:
            raise KindUnavailable(kind)
        from airlie.chat.repo_tools import build_repo_tools

        names = cfg.chat.kinds.repository.tools
        task = None
        if task_engine_for is not None and "task" in names:
            from airlie.chat.task_tool import Task

            candidate = Task(cfg, task_engine_for, library=library, chat_skills=skills_extra)
            task = candidate if candidate.usable else None  # no role to run it on: no tool
        registry = build_repo_tools(repo, ctx, names, task=task)
    else:
        names = cfg.chat.kinds.plain.tools
        registry = ToolRegistry([], ctx)
    tools: list[Tool] = []
    for name in names:
        if name in ("web_search", "web_fetch"):
            if name in web:
                tools.append(web[name])
        elif name == "skill":
            if cfg.chat.superpowers.enabled and library is not None:
                tools.append(SkillTool(library, skills_extra))
        elif name == "todo":
            tools.append(TodoTool(todos))
    for tool in tools:
        registry.add(tool)
    return registry
