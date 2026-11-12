"""The `task` tool: a fresh-context subagent on a chosen role, in the session's VM. A scripted FakeEngine per
role, a FakeBox for the VM and a real Manager over a fake engine resource for the hold."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.chat.repo_tools import build_repo_tools
from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.task_tool import Task, subagent_tools
from airlie.config import AirlieConfig, load_config
from airlie.engine.fake import FakeEngine, FakeReply
from airlie.engine.types import ToolCall
from airlie.manager.client import InProcessHolder, ManagedEngine
from airlie.manager.core import Manager
from airlie.serve.bridge.clock import FakeClock
from airlie.skills.library import prompt_text
from airlie.tools.protocol import ToolContext
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
    engine = FakeEngine({"coder": [FakeReply(text="  all done  ")]})
    result = await task(cfg, engine).run({"role": "coder", "prompt": "fix the bug"}, ctx_for(vm()))
    (req,) = engine.requests
    assert (result.ok, result.output) == (True, "all done")
    assert [(m.role, m.content) for m in req.messages] == [
        ("system", prompt_text(cfg.roles["coder"].prompt).rstrip() + "\n"),
        ("user", "fix the bug"),
    ]
    assert req.role == "coder"


async def test_task_tools_run_in_the_same_vm(cfg: AirlieConfig) -> None:
    box = vm()
    engine = FakeEngine(
        {"coder": [call("write_file", path="new.txt", content="hi\n"), FakeReply(text="wrote it")]}
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
    engine = FakeEngine({"coder": [FakeReply(text="ok")]})
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
    engine = FakeEngine({"coder": [FakeReply(text="by the coder")]})
    result = await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm()))
    assert (result.output, engine.requests[0].role) == ("by the coder", "coder")


async def test_task_with_an_unknown_role_lists_the_allowed_ones(cfg: AirlieConfig) -> None:
    engine = FakeEngine({})
    result = await task(cfg, engine).run({"role": "chief", "prompt": "go"}, ctx_for(vm()))
    assert result.ok is False
    assert "unknown role 'chief'" in result.output
    assert "coder, reviewer, tester, architect" in result.output
    assert engine.requests == []


async def test_task_with_no_prompt_is_an_error(cfg: AirlieConfig) -> None:
    result = await task(cfg, FakeEngine({})).run({"role": "coder"}, ctx_for(vm()))
    assert (result.ok, result.output) == (False, "error: prompt is required")


async def test_task_stops_at_max_steps_and_still_returns_an_answer(cfg: AirlieConfig) -> None:
    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.max_steps = 2
    engine = FakeEngine(
        {"coder": [call("list_dir", path="."), call("list_dir", path="."), FakeReply(text="best effort")]}
    )
    result = await task(cfg, engine).run({"prompt": "loop"}, ctx_for(vm()))
    assert (result.ok, result.output) == (True, "best effort")
    assert [bool(r.tools) for r in engine.requests] == [True, True, False]


async def test_task_skips_the_superpowers_bootstrap(cfg: AirlieConfig) -> None:
    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.roles = ["coder", "chat"]
    library = SuperpowersLibrary(ROOT / "vendor/superpowers")
    engine = FakeEngine({"chat": [FakeReply(text="ok")]})
    await Task(cfg, lambda role: engine, library=library).run({"role": "chat", "prompt": "x"}, ctx_for(vm()))
    system = engine.requests[0].messages[0].content
    assert library.bootstrap().strip()[:40] not in system
    assert "## Skills" not in system
    assert prompt_text(cfg.roles["chat"].prompt).strip()[:40] in system


async def test_task_caps_its_answer_at_the_output_cap(cfg: AirlieConfig) -> None:
    engine = FakeEngine({"coder": [FakeReply(text="x" * 50)]})
    result = await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm(), output_cap_chars=10))
    assert result.output == "x" * 10 + "\n[the answer was cut]"


async def test_task_with_no_answer_says_so(cfg: AirlieConfig) -> None:
    engine = FakeEngine({"coder": [FakeReply(text="")]})
    result = await task(cfg, engine).run({"prompt": "go"}, ctx_for(vm()))
    assert (result.ok, result.output) == (True, "(the subagent gave no answer)")


async def test_a_failed_engine_is_an_error_result_not_a_raise(cfg: AirlieConfig) -> None:
    result = await task(cfg, FakeEngine({})).run({"prompt": "go"}, ctx_for(vm()))
    assert result.ok is False
    assert "the coder subagent failed" in result.output and "no script" in result.output


async def test_a_context_without_a_vm_is_an_error_result(cfg: AirlieConfig) -> None:
    result = await task(cfg, FakeEngine({})).run({"prompt": "go"}, ToolContext(worktree=Path(VM)))
    assert result.ok is False and "need a box" in result.output


def test_the_schema_offers_the_configured_roles_and_requires_the_prompt(cfg: AirlieConfig) -> None:
    t = task(cfg, FakeEngine({}))
    assert t.parameters["properties"]["role"]["enum"] == ["coder", "reviewer", "tester", "architect"]
    assert t.parameters["required"] == ["prompt"]


def test_a_role_the_config_lacks_is_not_offered(cfg: AirlieConfig) -> None:
    cfg = cfg.model_copy(deep=True)
    cfg.chat.task.roles = ["coder", "nonesuch"]
    assert task(cfg, FakeEngine({})).roles == ["coder"]


def test_build_repo_tools_registers_task_only_when_named_and_given(cfg: AirlieConfig) -> None:
    t = task(cfg, FakeEngine({}))
    chat: Any = None  # push_branch and open_pr are not named, so the session is never read
    ctx = ctx_for(vm())
    assert "task" in build_repo_tools(chat, ctx, ["task", "grep"], task=t).names()
    assert "task" not in build_repo_tools(chat, ctx, ["grep"], task=t).names()
    assert "task" not in build_repo_tools(chat, ctx, ["task", "grep"]).names()
