"""The per-kind chat tool registry."""

from pathlib import Path
from typing import Any

import pytest

from airlie.chat.kinds import (
    KNOWN_CHAT_TOOLS,
    PLAIN_TOOLS,
    REPOSITORY_TOOLS,
    KindUnavailable,
    ask_first,
    registry_for,
)
from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.tools import TodoList
from airlie.config import AirlieConfig, load_config
from airlie.tools.protocol import ToolContext, ToolResult

ROOT = Path(__file__).resolve().parents[2]
CTX = ToolContext(worktree=Path("."))
FORBIDDEN = {
    "read_file", "write_file", "edit_file", "list_dir", "grep", "shell", "run_tests", "git", "task",
    "push_branch", "open_pr",
}  # fmt: skip


class _Web:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.parameters: dict[str, Any] = {}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(True, "")


WEB = {"web_search": _Web("web_search"), "web_fetch": _Web("web_fetch")}


@pytest.fixture(scope="module")
def cfg() -> AirlieConfig:
    return load_config(ROOT / "airlie.yaml", ROOT / "no-such-local.yaml")


@pytest.fixture
def library() -> SuperpowersLibrary:
    return SuperpowersLibrary(ROOT / "vendor" / "superpowers")


def test_plain_registry_has_exactly_the_configured_tools(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    reg = registry_for("plain", cfg, library=library, todos=TodoList(), ctx=CTX, web=WEB)

    assert reg.names() == ["web_search", "web_fetch", "skill", "todo"]
    assert reg.ctx is CTX


def test_plain_registry_has_no_file_shell_or_task_tool(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    reg = registry_for("plain", cfg, library=library, todos=TodoList(), ctx=CTX, web=WEB)

    assert not set(reg.names()) & FORBIDDEN
    assert not PLAIN_TOOLS & FORBIDDEN
    assert {"web_search", "web_fetch", "skill", "todo"} == PLAIN_TOOLS
    assert REPOSITORY_TOOLS == KNOWN_CHAT_TOOLS and FORBIDDEN <= REPOSITORY_TOOLS


def test_plain_registry_leaves_out_web_tools_when_no_search_is_configured(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    only_fetch = {"web_fetch": WEB["web_fetch"]}

    none = registry_for("plain", cfg, library=library, todos=TodoList(), ctx=CTX, web={})
    some = registry_for("plain", cfg, library=library, todos=TodoList(), ctx=CTX, web=only_fetch)

    assert none.names() == ["skill", "todo"]
    assert some.names() == ["web_fetch", "skill", "todo"]


def test_plain_registry_leaves_out_skill_when_superpowers_is_disabled(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    off = cfg.model_copy(deep=True)
    off.chat.superpowers.enabled = False

    disabled = registry_for("plain", off, library=library, todos=TodoList(), ctx=CTX, web=WEB)
    no_library = registry_for("plain", cfg, library=None, todos=TodoList(), ctx=CTX, web=WEB)

    assert disabled.names() == ["web_search", "web_fetch", "todo"]
    assert no_library.names() == ["web_search", "web_fetch", "todo"]


def test_the_todo_tool_of_a_registry_writes_the_list_it_was_given(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    import asyncio

    from airlie.engine.types import ToolCall

    todos = TodoList()
    reg = registry_for("plain", cfg, library=library, todos=todos, ctx=CTX, web=WEB)

    result = asyncio.run(
        reg.call(ToolCall("1", "todo", '{"items": [{"id": "a", "text": "x", "status": "pending"}]}'))
    )

    assert result.ok and len(todos.items()) == 1


def test_a_repository_registry_needs_an_opened_session(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    with pytest.raises(KindUnavailable) as err:
        registry_for("repository", cfg, library=library, todos=TodoList(), ctx=CTX, web=WEB)

    assert err.value.kind == "repository"
    assert str(err.value) == "the repository chat kind needs a repository session to build its tools"


def test_ask_first_is_read_from_the_kind_config(cfg: AirlieConfig) -> None:
    changed = cfg.model_copy(deep=True)
    changed.chat.kinds.plain.ask_first = ["web_fetch"]
    changed.chat.kinds.repository.ask_first = ["shell", "git"]

    assert ask_first("plain", changed) == frozenset({"web_fetch"})
    assert ask_first("repository", changed) == frozenset({"shell", "git"})
