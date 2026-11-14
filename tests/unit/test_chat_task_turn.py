"""A chat turn that calls `task`: the subagent's own tool calls reach the audit hook marked with the task
call, and while it waits for memory the manager's status for its engine goes to the client. A real Manager
over fake resources; the chat and subagent engines are scripted."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from airlie.chat.task_tool import Task
from airlie.config import AirlieConfig, load_config
from airlie.config.schema import RoleSpec
from airlie.engine.fake import FakeEngine, FakeReply
from airlie.engine.types import ToolCall
from airlie.manager.client import InProcessHolder, ManagedEngine
from airlie.manager.core import Manager
from airlie.serve.bridge.clock import FakeClock
from airlie.tools.protocol import ToolContext
from airlie.tools.registry import ToolRegistry
from tests.support.box import FakeBox
from tests.unit.test_chat_turn import Rig, Step, collect, parse, reply, words
from tests.unit.test_manager import FakeResource, settle

ROOT = Path(__file__).resolve().parents[2]
VM = "/home/admin/work"


@pytest.fixture(scope="module")
def cfg() -> AirlieConfig:
    cfg = load_config(ROOT / "airlie.yaml", ROOT / "no-such-local.yaml").model_copy(deep=True)
    cfg.chat.task.roles = ["coder"]
    return cfg


@pytest.fixture
def role() -> RoleSpec:
    return RoleSpec.model_validate({"prompt": "p.md", "model": "m"})


def chat_calls_task() -> list[Step]:
    call = ToolCall("c1", "task", json.dumps({"prompt": "write a file"}))
    return [Step(reply(calls=[call])), Step(reply("all done"), words("all done"))]


def write_then_answer() -> FakeEngine:
    write = ToolCall("w1", "write_file", json.dumps({"path": "a.txt", "content": "hi\n"}))
    return FakeEngine({"architect": [FakeReply(tool_calls=[write]), FakeReply(text="wrote a.txt")]})


def with_task(rig: Rig, cfg: AirlieConfig, engine: Any) -> FakeBox:
    box = FakeBox("vm", state="running")
    ctx = ToolContext(worktree=Path(VM), box=box)
    rig.registry = ToolRegistry([Task(cfg, lambda r: engine)], ctx)
    return box


async def test_a_subagents_tool_calls_reach_the_audit_hook_marked_with_the_task_call(
    tmp_path: Path, role: RoleSpec, cfg: AirlieConfig
) -> None:
    rig = Rig(tmp_path, role, chat_calls_task())
    box = with_task(rig, cfg, write_then_answer())

    await collect(rig.turn())

    assert [(e.name, e.call_id, e.via, e.ok) for e in rig.tool_events] == [
        ("write_file", "w1", "task:c1", True),
        ("task", "c1", "", True),
    ]
    assert box.files[f"{VM}/a.txt"] == b"hi\n"


async def test_a_turn_without_a_task_call_marks_no_call_as_a_subagents(
    tmp_path: Path, role: RoleSpec
) -> None:
    rig = Rig(tmp_path, role, [Step(reply("hi"), words("hi"))])

    await collect(rig.turn())

    assert [e.via for e in rig.tool_events] == []


async def test_the_status_of_an_engine_a_subagent_waits_on_is_relayed_with_its_reason(
    tmp_path: Path, role: RoleSpec, cfg: AirlieConfig
) -> None:
    sub = cfg.engine_for_role("coder")
    clock = FakeClock()
    manager = Manager(
        [FakeResource("chat-engine"), FakeResource(sub), FakeResource("other")], budget_gb=25.0, clock=clock
    )
    rig = Rig(tmp_path, role, chat_calls_task())
    rig.holder = InProcessHolder(manager)  # type: ignore[assignment]
    rig.status = manager.status  # type: ignore[method-assign]
    engine = ManagedEngine(
        FakeEngine({"architect": [FakeReply(text="reviewed")]}), sub, InProcessHolder(manager)
    )
    with_task(rig, cfg, engine)
    release = asyncio.Event()

    async def hold_other() -> None:
        async with manager.hold("other", "story:1"):
            await release.wait()

    async def let_go_once_waiting() -> None:
        while not any(s.name == sub and s.state == "waiting" for s in manager.status()):
            await asyncio.sleep(0)
        release.set()

    blocker = asyncio.create_task(hold_other())
    await asyncio.sleep(0)
    releaser = asyncio.create_task(let_go_once_waiting())

    chunks = parse(await asyncio.wait_for(collect(rig.turn()), 5))
    await asyncio.gather(blocker, releaser)

    shown = [
        (c["data"]["engine"], c["data"]["state"], c["data"]["detail"])
        for c in chunks
        if c["type"] == "data-engine"
    ]
    assert (sub, "waiting", "waiting for memory: chat:t1, story:1") in shown
    assert [c["output"] for c in chunks if c["type"] == "tool-output-available"] == ["reviewed"]


async def test_a_status_relay_that_fails_never_replaces_the_tasks_result(
    tmp_path: Path, role: RoleSpec, cfg: AirlieConfig
) -> None:
    rig = Rig(tmp_path, role, chat_calls_task())
    with_task(rig, cfg, write_then_answer())

    def broken_status() -> list[Any]:
        raise RuntimeError("the manager's status could not be read")

    rig.status = broken_status  # type: ignore[method-assign]

    chunks = parse(await asyncio.wait_for(collect(rig.turn()), 5))

    assert [c["output"] for c in chunks if c["type"] == "tool-output-available"] == ["wrote a.txt"]
    assert not any(c["type"] == "error" for c in chunks)


async def test_the_status_relay_stops_when_the_task_call_ends(
    tmp_path: Path, role: RoleSpec, cfg: AirlieConfig
) -> None:
    rig = Rig(tmp_path, role, chat_calls_task())
    with_task(rig, cfg, write_then_answer())
    await collect(rig.turn())
    polls = rig.holder.polls

    await settle()  # a relay still running would read the status again here

    assert rig.holder.polls == polls
