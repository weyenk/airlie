"""AutoGen team adapter: events map to StepRecords; tools wrap the registry; client targets the engine."""

from __future__ import annotations

from pathlib import Path

from autogen_agentchat.messages import ToolCallExecutionEvent, ToolCallRequestEvent
from autogen_core import FunctionCall
from autogen_core.models import FunctionExecutionResult

from airlie.config import RoleSpec
from airlie.engine import ToolCall
from airlie.teams.autogen_team import build_function_tools, build_model_client, pair_steps
from airlie.tools import ToolContext, ToolRegistry


def test_pair_steps_matches_requests_with_results() -> None:
    req = ToolCallRequestEvent(
        source="coder", content=[FunctionCall(id="c1", name="read_file", arguments='{"path": "a"}')]
    )
    res = ToolCallExecutionEvent(
        source="coder",
        content=[FunctionExecutionResult(call_id="c1", name="read_file", content="1: x", is_error=False)],
    )
    steps = pair_steps("team-1", 0, [req, res], assistant_text="")
    assert len(steps) == 1
    s = steps[0]
    assert s.team_id == "team-1" and s.step_num == 1
    assert s.u_t == ToolCall("c1", "read_file", '{"path": "a"}')
    assert s.o_t == "1: x" and s.ok is True


def test_pair_steps_marks_errors_and_orphans() -> None:
    req = ToolCallRequestEvent(
        source="coder",
        content=[
            FunctionCall(id="c1", name="shell", arguments="{}"),
            FunctionCall(id="c2", name="shell", arguments="{}"),
        ],
    )
    res = ToolCallExecutionEvent(
        source="coder",
        content=[FunctionExecutionResult(call_id="c1", name="shell", content="boom", is_error=True)],
    )
    steps = pair_steps("t", 5, [req, res], assistant_text="")
    assert [s.step_num for s in steps] == [6, 7]
    assert steps[0].ok is False and steps[0].o_t == "boom"
    assert steps[1].ok is False and "no result" in steps[1].o_t


async def test_function_tools_call_the_registry(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    reg = ToolRegistry.builtin(["write_file", "read_file"], ToolContext(worktree=wt))
    tools = build_function_tools(reg)
    assert [t.name for t in tools] == ["write_file", "read_file"]
    write = tools[0]
    out = await write.run_json({"path": "x.txt", "content": "hi"}, cancellation_token=None)  # type: ignore[arg-type]
    assert "wrote x.txt" in str(out)
    assert (wt / "x.txt").read_text() == "hi"
    schema = write.schema
    assert schema["parameters"]["required"] == ["path", "content"]


def test_model_client_targets_engine_and_role_model(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("x")
    role = RoleSpec(prompt=tmp_path / "p.md", model="base", thinking=False, max_tokens=64)
    client = build_model_client("http://127.0.0.1:8080/v1", "base", role, api_key=None)
    info = client.model_info
    assert info["function_calling"] is True and info["json_output"] is False
    assert client.create_stream is not None
    cfg = client.dump_component().config
    assert cfg["base_url"] == "http://127.0.0.1:8080/v1"
    assert cfg["model"] == "base"
    assert cfg["temperature"] == role.sampling.temperature


async def test_registry_tool_yields_slot_during_tool_execution(tmp_path: Path) -> None:
    """Spec 04 §2: a team inside a tool call must not hold a generation slot."""
    from airlie.scheduler import AgentState, SlotScheduler
    from airlie.teams.autogen_team import build_function_tools

    wt = tmp_path / "wt"
    wt.mkdir()
    sched = SlotScheduler(1)
    seen: list[AgentState] = []

    class SpyRegistry(ToolRegistry):
        async def call(self, call):  # type: ignore[override]
            seen.append(sched.state("team-1"))
            return await super().call(call)

    reg = SpyRegistry.builtin(["read_file"], ToolContext(worktree=wt))  # type: ignore[assignment]
    (wt / "a.txt").write_text("x")
    tools = build_function_tools(reg, scheduler=sched, team_id="team-1")
    await tools[0].run_json({"path": "a.txt"}, cancellation_token=None)  # type: ignore[arg-type]
    assert seen == [AgentState.SUSPENDED_TOOL_STALL]
    assert sched.metrics().yield_count == 1
    assert sched.state("team-1") == AgentState.ACTIVE


def test_model_client_takes_a_generation_slot_per_call(tmp_path: Path) -> None:
    from airlie.scheduler import SlotScheduler
    from airlie.teams.autogen_client import RepairingOpenAIClient

    (tmp_path / "p.md").write_text("x")
    role = RoleSpec(prompt=tmp_path / "p.md", model="base", thinking=False, max_tokens=64)
    sched = SlotScheduler(2)
    client = build_model_client(
        "http://127.0.0.1:1/v1", "base", role, api_key=None, scheduler=sched, team_id="t"
    )
    assert isinstance(client, RepairingOpenAIClient)
    assert client.slot_scheduler is sched and client.team_id == "t"
