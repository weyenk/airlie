"""The `task` tool: a fresh-context subagent on a chosen role, in the session's VM. A scripted FakeEngine per
role, a FakeBox for the VM and a real Manager over a fake engine resource for the hold."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from airlie.chat import task_tool
from airlie.chat.kinds import needs_yes
from airlie.chat.repo_tools import build_repo_tools
from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.task_tool import OBSERVER, Task, TaskObserver, subagent_tools
from airlie.config import AirlieConfig, load_config
from airlie.engine.fake import FakeEngine, FakeReply
from airlie.engine.types import ToolCall
from airlie.manager.client import InProcessHolder, ManagedEngine
from airlie.manager.core import Manager
from airlie.serve.bridge.clock import FakeClock
from airlie.skills.library import Skill, prompt_text
from airlie.tools.protocol import ToolContext, ToolResult
from tests.support.box import FakeBox
from tests.support.serve import FakeEngine as FakeResource

ROOT = Path(__file__).resolve().parents[2]
VM = "/home/admin/work"


@pytest.fixture(scope="module")
def cfg() -> AirlieConfig:
    return load_config(ROOT / "airlie.yaml", ROOT / "no-such-local.yaml")


def ctx_for(box: FakeBox, **kw: Any) -> ToolContext:
    return ToolContext(worktree=Path(VM), box=box, box_env={"CI": "1"}, **kw)


def vm() -> FakeBox:
    return FakeBox("vm", state="running")


def call(name: str, **args: Any) -> FakeReply:
    return FakeReply(tool_calls=[ToolCall("c1", name, json.dumps(args))])


def task(cfg: AirlieConfig, engine: FakeEngine) -> Task:
    return Task(cfg, lambda role: engine)


async def test_task_runs_a_fresh_context_and_returns_its_final_text(cfg: AirlieConfig) -> None:
    engine = FakeEngine({"architect": [FakeReply(text="  all done  ")]})
    result = await task(cfg, engine).run({"role": "architect", "prompt": "fix the bug"}, ctx_for(vm()))
    (req,) = engine.requests
    assert (result.ok, result.output) == (True, "all done")
    assert [(m.role, m.content) for m in req.messages] == [
        ("system", prompt_text(cfg.roles["architect"].prompt).rstrip() + "\n"),
        ("user", "fix the bug"),
    ]
    assert req.role == "architect"


async def test_task_tools_run_in_the_same_vm(cfg: AirlieConfig) -> None:
    box = vm()
    engine = FakeEngine(
        {"architect": [call("write_file", path="new.txt", content="hi\n"), FakeReply(text="wrote it")]}
    )
    result = await task(cfg, engine).run({"prompt": "write"}, ctx_for(box))
    assert (result.output, box.files[f"{VM}/new.txt"]) == ("wrote it", b"hi\n")


async def test_task_holds_its_roles_engine_through_the_manager(cfg: AirlieConfig) -> None:
    resource = FakeResource("main")
    manager = Manager([resource], budget_gb=100.0, clock=FakeClock())
    seen: list[tuple[str, ...]] = []

    class Spy(FakeEngine):
        async def generate(self, req: Any, on_token: Any = None) -> Any:
            seen.append(manager.status()[0].holders)
            return await super().generate(req, on_token)

    engine = ManagedEngine(
        Spy({"reviewer": [FakeReply(text="looks fine")]}), "main", InProcessHolder(manager)
    )
    assert resource.up is False  # a stopped reviewer
    result = await Task(cfg, lambda role: engine).run({"role": "reviewer", "prompt": "review"}, ctx_for(vm()))
    assert (result.output, seen, resource.up) == ("looks fine", [("role:reviewer",)], True)
    assert manager.status()[0].holders == ()


async def test_task_has_no_task_push_or_pr_tool_and_none_the_kind_asks_first(cfg: AirlieConfig) -> None:
    engine = FakeEngine({"architect": [FakeReply(text="ok")]})
    cfg = cfg.model_copy(deep=True)
    cfg.chat.kinds.repository.ask_first = ["push_branch", "open_pr", "build_image", "shell"]
    await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm()))
    offered = {t.name for t in engine.requests[0].tools or []}
    assert offered == {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "grep",
        "run_tests",
        "git",
        "compute",
    }
    assert set(subagent_tools(cfg)) == offered


async def test_task_without_a_role_uses_the_default_role(cfg: AirlieConfig) -> None:
    engine = FakeEngine({"architect": [FakeReply(text="by the architect")]})
    result = await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm()))
    assert (result.output, engine.requests[0].role) == ("by the architect", "architect")


async def test_task_with_an_unknown_role_lists_the_allowed_ones(cfg: AirlieConfig) -> None:
    engine = FakeEngine({})
    result = await task(cfg, engine).run({"role": "chief", "prompt": "go"}, ctx_for(vm()))
    assert result.ok is False
    assert "unknown role 'chief'" in result.output
    assert "architect, reviewer, product" in result.output
    assert engine.requests == []


async def test_task_with_no_prompt_is_an_error(cfg: AirlieConfig) -> None:
    result = await task(cfg, FakeEngine({})).run({"role": "architect"}, ctx_for(vm()))
    assert (result.ok, result.output) == (False, "error: prompt is required")


async def test_task_stops_at_max_steps_and_still_returns_an_answer(cfg: AirlieConfig) -> None:
    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.max_steps = 2
    engine = FakeEngine(
        {"architect": [call("list_dir", path="."), call("list_dir", path="."), FakeReply(text="best effort")]}
    )
    result = await task(cfg, engine).run({"prompt": "loop"}, ctx_for(vm()))
    assert (result.ok, result.output) == (True, "best effort")
    assert [bool(r.tools) for r in engine.requests] == [True, True, False]


async def test_task_skips_the_superpowers_bootstrap(cfg: AirlieConfig) -> None:
    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.roles = ["architect", "chat"]
    library = SuperpowersLibrary(ROOT / "vendor/superpowers")
    engine = FakeEngine({"chat": [FakeReply(text="ok")]})
    await Task(cfg, lambda role: engine, library=library).run({"role": "chat", "prompt": "x"}, ctx_for(vm()))
    system = engine.requests[0].messages[0].content
    assert library.bootstrap().strip()[:40] not in system
    assert "## Skills" not in system
    assert prompt_text(cfg.roles["chat"].prompt).strip()[:40] in system


async def test_task_caps_its_answer_at_the_output_cap(cfg: AirlieConfig) -> None:
    engine = FakeEngine({"architect": [FakeReply(text="x" * 50)]})
    result = await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm(), output_cap_chars=10))
    assert result.output == "x" * 10 + "\n[the answer was cut]"


async def test_task_with_no_answer_says_so(cfg: AirlieConfig) -> None:
    engine = FakeEngine({"architect": [FakeReply(text="")]})
    result = await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm()))
    assert (result.ok, result.output) == (True, "(the subagent gave no answer)")


async def test_a_failed_engine_is_an_error_result_not_a_raise(cfg: AirlieConfig) -> None:
    result = await task(cfg, FakeEngine({})).run({"prompt": "go"}, ctx_for(vm()))
    assert result.ok is False
    assert "the architect subagent failed" in result.output and "no script" in result.output


async def test_a_context_without_a_vm_is_an_error_result(cfg: AirlieConfig) -> None:
    result = await task(cfg, FakeEngine({})).run({"prompt": "go"}, ToolContext(worktree=Path(VM)))
    assert result.ok is False and "need a box" in result.output


def test_the_schema_offers_the_configured_roles_and_requires_the_prompt(cfg: AirlieConfig) -> None:
    t = task(cfg, FakeEngine({}))
    assert t.parameters["properties"]["role"]["enum"] == ["architect", "reviewer", "product"]
    assert t.parameters["required"] == ["prompt"]


def test_a_role_the_config_lacks_is_not_offered(cfg: AirlieConfig) -> None:
    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.roles = ["architect", "nonesuch"]
    assert task(cfg, FakeEngine({})).roles == ["architect"]


@pytest.mark.parametrize(
    ("names", "given", "present"),
    [(["task", "grep"], True, True), (["grep"], True, False), (["task", "grep"], False, False)],
)
def test_build_repo_tools_registers_task_only_when_named_and_given(
    cfg: AirlieConfig, names: list[str], given: bool, present: bool
) -> None:
    chat: Any = None  # push_branch and open_pr are not named, so the session is never read
    registry = build_repo_tools(chat, ctx_for(vm()), names, task=task(cfg, FakeEngine({})) if given else None)
    assert ("task" in registry.names()) is present


def test_a_boxed_tool_named_like_a_forbidden_one_is_still_not_offered(
    cfg: AirlieConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.tools.fs import ReadFile

    forbidden = ["task", "push_branch", "open_pr", "skill", "todo"]
    monkeypatch.setattr(task_tool, "BOXED", {**task_tool.BOXED, **{n: ReadFile for n in forbidden}})
    cfg = cfg.model_copy(deep=True)
    cfg.chat.kinds.repository.tools = ["read_file", *forbidden]
    assert subagent_tools(cfg) == ["read_file"]


def test_a_tool_the_guard_always_asks_for_is_not_offered_even_if_boxed(
    cfg: AirlieConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.tools.fs import ReadFile

    monkeypatch.setattr(task_tool, "BOXED", {**task_tool.BOXED, "build_image": ReadFile})
    cfg = cfg.model_copy(deep=True)
    cfg.chat.kinds.repository.tools = ["read_file", "build_image"]
    assert needs_yes("repository", cfg) >= {"push_branch", "open_pr", "build_image"}
    assert subagent_tools(cfg) == ["read_file"]


async def test_a_stop_during_a_subagent_cancels_its_generation_and_releases_its_hold(
    cfg: AirlieConfig,
) -> None:
    resource = FakeResource("main")
    manager = Manager([resource], budget_gb=100.0, clock=FakeClock())
    started = asyncio.Event()

    class Hangs(FakeEngine):
        async def generate(self, req: Any, on_token: Any = None) -> Any:
            started.set()
            await asyncio.Event().wait()

    inner = Hangs({})
    engine = ManagedEngine(inner, "main", InProcessHolder(manager))
    running = asyncio.create_task(
        Task(cfg, lambda role: engine).run({"role": "reviewer", "prompt": "review"}, ctx_for(vm()))
    )
    await started.wait()
    assert manager.status()[0].holders == ("role:reviewer",)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert manager.status()[0].holders == ()
    assert len(inner.cancelled) == 1


async def test_each_subagent_call_is_reported_to_the_observer(cfg: AirlieConfig) -> None:
    seen: list[tuple[str, bool]] = []

    async def after_call(call: ToolCall, result: ToolResult) -> None:
        seen.append((call.id, result.ok))

    engine = FakeEngine({"architect": [call("write_file", path="a", content="x"), FakeReply(text="ok")]})
    token = OBSERVER.set(TaskObserver(after_call))
    try:
        await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm()))
    finally:
        OBSERVER.reset(token)
    assert seen == [("c1", True)]


@pytest.mark.parametrize("roles", [[], ["nonesuch"]])
def test_a_task_with_no_role_the_config_has_is_not_usable(cfg: AirlieConfig, roles: list[str]) -> None:
    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.roles = roles
    assert task(cfg, FakeEngine({})).usable is False


def test_a_task_whose_default_role_is_not_offered_is_not_usable(cfg: AirlieConfig) -> None:
    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.roles = ["reviewer"]
    cfg.chat.task.default_role = "architect"
    assert task(cfg, FakeEngine({})).usable is False


def test_the_engines_of_its_roles_are_listed_for_the_status_relay(cfg: AirlieConfig) -> None:
    assert task(cfg, FakeEngine({})).engines == sorted(
        {cfg.engine_for_role(r) for r in ["architect", "reviewer", "product"]}
    )


def test_a_subagent_is_offered_a_boxed_tool_the_kind_never_asks_about_but_not_the_always_asked_ones(
    cfg: AirlieConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.tools.fs import ReadFile

    monkeypatch.setattr(task_tool, "BOXED", {**task_tool.BOXED, "build_image": ReadFile, "grep": ReadFile})
    cfg = cfg.model_copy(deep=True)
    cfg.chat.kinds.repository.tools = ["read_file", "build_image", "grep"]
    cfg.chat.kinds.repository.ask_first = ["grep"]  # build_image is not listed: the guard asks anyway

    assert subagent_tools(cfg) == ["read_file"]


def test_the_description_names_the_roles_and_the_default(cfg: AirlieConfig) -> None:
    t = task(cfg, FakeEngine({}))

    assert t.description == (
        "Run a subagent with a fresh context on a role, in this VM, and get its final answer back. It sees "
        "only the prompt you give it (write it so it stands alone: what to do, which files, what to "
        "return), has the file, shell and git tools but cannot push, open a pull request or start another "
        "task. Roles: architect, reviewer, product; none means architect."
    )


def test_the_parameters_are_an_object_of_a_role_and_a_prompt(cfg: AirlieConfig) -> None:
    t = task(cfg, FakeEngine({}))

    assert t.parameters == {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "enum": ["architect", "reviewer", "product"],
                "description": "the role to run the task as",
            },
            "prompt": {"type": "string", "description": "the task, complete in itself"},
        },
        "required": ["prompt"],
    }


async def test_a_chat_role_subagent_gets_the_slash_command_list_but_no_bootstrap(
    cfg: AirlieConfig, tmp_path: Path
) -> None:
    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.roles = ["architect", "chat"]
    skills = {"story": Skill("story", "Draft a story", "BODY", tmp_path / "story.md")}
    engine = FakeEngine({"chat": [FakeReply(text="ok")]})
    t = Task(cfg, lambda role: engine, chat_skills=skills)

    await t.run({"role": "chat", "prompt": "x"}, ctx_for(vm()))

    assert "## Slash commands\n\n- /story: Draft a story\n" in engine.requests[0].messages[0].content


async def test_a_role_other_than_chat_gets_only_its_own_prompt_even_with_skills(
    cfg: AirlieConfig, tmp_path: Path
) -> None:
    skills = {"story": Skill("story", "Draft a story", "BODY", tmp_path / "story.md")}
    engine = FakeEngine({"architect": [FakeReply(text="ok")]})

    await Task(cfg, lambda role: engine, chat_skills=skills).run({"prompt": "x"}, ctx_for(vm()))

    assert "Slash commands" not in engine.requests[0].messages[0].content


async def test_the_subagent_generates_with_its_roles_own_sampling_limits_and_thinking(
    cfg: AirlieConfig,
) -> None:
    cfg = cfg.model_copy(deep=True)
    spec = cfg.roles["architect"]
    spec.thinking = True
    spec.max_tokens = 1234
    engine = FakeEngine({"architect": [FakeReply(text="ok")]})

    await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm()))

    (req,) = engine.requests
    assert (req.sampling, req.max_tokens, req.thinking) == (spec.sampling, 1234, True)


async def test_the_engine_is_chosen_for_the_role_that_runs(cfg: AirlieConfig) -> None:
    asked: list[str] = []
    engine = FakeEngine({"reviewer": [FakeReply(text="ok")]})

    def engine_for(role: str) -> FakeEngine:
        asked.append(role)
        return engine

    await Task(cfg, engine_for).run({"role": "reviewer", "prompt": "go"}, ctx_for(vm()))

    assert asked == ["reviewer"]


async def test_a_stop_cancels_the_generation_it_started_by_its_request_id(cfg: AirlieConfig) -> None:
    started = asyncio.Event()
    seen: list[str] = []

    class Hangs(FakeEngine):
        async def generate(self, req: Any, on_token: Any = None) -> Any:
            seen.append(req.request_id)
            started.set()
            await asyncio.Event().wait()

    engine = Hangs({})
    running = asyncio.create_task(task(cfg, engine).run({"prompt": "go"}, ctx_for(vm())))
    await started.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert engine.cancelled == seen


async def test_a_stop_still_propagates_when_cancelling_the_engine_itself_fails(cfg: AirlieConfig) -> None:
    started = asyncio.Event()

    class Hangs(FakeEngine):
        async def generate(self, req: Any, on_token: Any = None) -> Any:
            started.set()
            await asyncio.Event().wait()

        async def cancel(self, request_id: str) -> None:
            raise RuntimeError("engine already gone")

    running = asyncio.create_task(task(cfg, Hangs({})).run({"prompt": "go"}, ctx_for(vm())))
    await started.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


async def test_an_answer_exactly_at_the_output_cap_is_kept_whole(cfg: AirlieConfig) -> None:
    engine = FakeEngine({"architect": [FakeReply(text="x" * 10)]})

    result = await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm(), output_cap_chars=10))

    assert result.output == "x" * 10
