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


# ---- the task tool in a repository registry ----


def _repository(cfg: AirlieConfig, library: SuperpowersLibrary, **kw: Any) -> list[str]:
    from airlie.tools.protocol import ToolContext
    from tests.support.box import FakeBox

    ctx = ToolContext(worktree=Path("/w"), box=FakeBox("vm", state="running"))
    chat: Any = object()  # push_branch and open_pr only keep it
    reg = registry_for(
        "repository", cfg, library=library, todos=TodoList(), ctx=ctx, web=WEB, repo=chat, **kw
    )
    return reg.names()


def test_a_repository_registry_holds_task_when_it_has_an_engine_for_it(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    from airlie.engine.fake import FakeEngine

    assert "task" in _repository(cfg, library, task_engine_for=lambda role: FakeEngine({}))


def test_a_repository_registry_without_an_engine_for_it_has_no_task(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    assert "task" not in _repository(cfg, library)


def test_a_repository_registry_has_no_task_when_the_kind_does_not_list_it(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    from airlie.engine.fake import FakeEngine

    cfg = cfg.model_copy(deep=True)
    cfg.chat.kinds.repository.tools = ["read_file", "grep"]
    assert "task" not in _repository(cfg, library, task_engine_for=lambda role: FakeEngine({}))


def test_a_repository_registry_has_no_task_when_no_role_of_it_exists(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    from airlie.engine.fake import FakeEngine

    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.roles = ["nonesuch"]
    assert "task" not in _repository(cfg, library, task_engine_for=lambda role: FakeEngine({}))


@pytest.mark.parametrize("kind", ["plain", "repository"])
def test_needs_yes_is_the_kinds_ask_first_and_for_a_repository_the_always_asked(
    cfg: AirlieConfig, kind: str
) -> None:
    from airlie.chat.kinds import needs_yes

    cfg = cfg.model_copy(deep=True)
    cfg.chat.kinds.plain.ask_first = ["todo"]
    got = needs_yes(kind, cfg)  # type: ignore[arg-type]
    assert got == (frozenset({"todo"}) if kind == "plain" else {"push_branch", "open_pr", "build_image"})


async def test_the_registrys_task_tool_runs_on_the_engine_for_its_role_with_the_chat_skills(
    cfg: AirlieConfig, library: SuperpowersLibrary, tmp_path: Path
) -> None:
    import json

    from airlie.engine.fake import FakeEngine, FakeReply
    from airlie.engine.types import ToolCall
    from airlie.skills.library import Skill
    from tests.support.box import FakeBox

    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.roles = ["coder", "chat"]
    engine = FakeEngine({"chat": [FakeReply(text="done")]})
    asked: list[str] = []

    def engine_for(role: str) -> FakeEngine:
        asked.append(role)
        return engine

    ctx = ToolContext(worktree=Path("/w"), box=FakeBox("vm", state="running"))
    skills = {"story": Skill("story", "Draft a story", "BODY", tmp_path / "story.md")}
    reg = registry_for(
        "repository",
        cfg,
        library=library,
        todos=TodoList(),
        ctx=ctx,
        web=WEB,
        skills_extra=skills,
        repo=object(),  # type: ignore[arg-type]
        task_engine_for=engine_for,
    )

    result = await reg.call(ToolCall("c1", "task", json.dumps({"role": "chat", "prompt": "x"})))

    assert (result.ok, result.output, asked) == (True, "done", ["chat"])
    assert "- /story: Draft a story" in engine.requests[0].messages[0].content


async def test_the_registrys_push_tool_belongs_to_the_session_it_was_given(
    cfg: AirlieConfig, library: SuperpowersLibrary
) -> None:
    from airlie.boxes.transit import TransitError
    from tests.support.box import FakeBox

    class Mirror:
        can_push = False

    class Session:
        mirror = Mirror()

    ctx = ToolContext(worktree=Path("/w"), box=FakeBox("vm", state="running"))
    reg = registry_for(
        "repository",
        cfg,
        library=library,
        todos=TodoList(),
        ctx=ctx,
        web=WEB,
        repo=Session(),  # type: ignore[arg-type]
    )
    push: Any = reg.get("push_branch")

    with pytest.raises(TransitError):
        await push.subject({})
