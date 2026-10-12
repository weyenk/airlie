"""Native ReAct team loop over the Engine seam: tool calls become steps, observers see (u_t, o_t)."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.config import RoleSpec
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.scheduler import SlotScheduler
from airlie.teams import NativeTeam, StepObserver, StepRecord, TeamContext
from airlie.tools import ToolContext, ToolRegistry


class Recorder(StepObserver):
    def __init__(self, inject: bool = False) -> None:
        self.steps: list[StepRecord] = []
        self.injections: list[tuple[str, int]] = []
        self.inject = inject

    async def before_step(self, team_id: str, step_num: int) -> str | None:
        self.injections.append((team_id, step_num))
        return (
            "[Shared Memory Keys Available]:\n- Key [m1]: nothing yet"
            if self.inject and step_num == 2
            else None
        )

    async def after_step(self, step: StepRecord) -> None:
        self.steps.append(step)


def make_team(tmp_path: Path, engine: FakeEngine, *, max_steps: int = 10, token_budget: int = 10**6):
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    (tmp_path / "coder.md").write_text("You are the coder.")
    role = RoleSpec(prompt=tmp_path / "coder.md", model="base", thinking=False, max_tokens=64)
    tools = ToolRegistry.builtin(
        ["write_file", "read_file", "run_tests"], ToolContext(worktree=wt, test_cmd="echo ok")
    )
    ctx = TeamContext(
        team_id="team-1",
        goal="make it pass",
        role=role,
        tools=tools,
        worktree=wt,
        max_steps=max_steps,
        token_budget=token_budget,
        scheduler=SlotScheduler(2),
    )
    return NativeTeam(engine), ctx


async def test_tool_calls_become_steps_and_final_text_ends_the_run(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(
                    tool_calls=[
                        ToolCall("c1", "write_file", json.dumps({"path": "a.py", "content": "x=1\n"}))
                    ]
                ),
                FakeReply(tool_calls=[ToolCall("c2", "run_tests", "{}")]),
                FakeReply(text="Done: wrote a.py and tests pass."),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    rec = Recorder()
    result = await team.run(ctx, rec)
    assert result.ok and result.final_text.startswith("Done")
    assert [s.u_t.name for s in rec.steps] == ["write_file", "run_tests"]
    assert rec.steps[0].o_t.startswith("wrote a.py")
    assert "ok" in rec.steps[1].o_t
    assert (tmp_path / "wt" / "a.py").read_text() == "x=1\n"
    assert result.steps == 2
    # the conversation resent to the engine grows: system, user, assistant(tool_calls), tool, ...
    last = engine.requests[-1].messages
    assert [m.role for m in last][:5] == ["system", "user", "assistant", "tool", "assistant"]
    assert last[2].tool_calls and last[3].tool_call_id == "c1"


async def test_observer_injection_is_appended_as_a_user_message(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "missing.py"}))]),
                FakeReply(text="giving up"),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    rec = Recorder(inject=True)
    await team.run(ctx, rec)
    msgs = engine.requests[-1].messages
    injected = [m for m in msgs if m.role == "user" and "Shared Memory Keys" in m.content]
    assert len(injected) == 1
    assert rec.injections == [("team-1", 1), ("team-1", 2)]


async def test_max_steps_stops_the_loop(tmp_path: Path) -> None:
    calls = [
        FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": f"f{i}"}))]) for i in range(10)
    ]
    engine = FakeEngine({"coder": calls})
    team, ctx = make_team(tmp_path, engine, max_steps=3)
    result = await team.run(ctx, Recorder())
    assert not result.ok and result.stop_reason == "max_steps"
    assert result.steps == 3


async def test_native_team_repeated_action_stops(tmp_path: Path) -> None:
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [same] * 10})
    team, ctx = make_team(tmp_path, engine, max_steps=10)
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "repeated_action"
    assert result.steps == 3  # three identical calls in a row


async def test_token_budget_stops_the_loop(tmp_path: Path) -> None:
    engine = FakeEngine(
        {"coder": [FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])] * 10}
    )
    team, ctx = make_team(tmp_path, engine, token_budget=5)
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "token_budget"


async def test_tools_are_sent_to_the_engine(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [FakeReply(text="ok")]})
    team, ctx = make_team(tmp_path, engine)
    await team.run(ctx, Recorder())
    assert [t.name for t in engine.requests[0].tools or []] == ["write_file", "read_file", "run_tests"]
    assert engine.requests[0].messages[0].content == "You are the coder."
    assert "make it pass" in engine.requests[0].messages[1].content


async def test_token_budget_counts_context_plus_generated_not_resent_history(tmp_path: Path) -> None:
    """Each turn resends the whole conversation (prefix-cached); the budget must not grow quadratically."""
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": f"f{i}"}))])
                for i in range(6)
            ]
            + [FakeReply(text="ok")]
        }
    )
    team, ctx = make_team(
        tmp_path, engine, token_budget=250
    )  # fake prompt_tokens ≈ words in messages (~20-40)
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done"  # cumulative resent prompts would exceed 60 long before step 6


async def test_repeated_action_requires_identical_outputs(tmp_path: Path) -> None:
    """run_tests three times with changing output is a fix loop, not a stuck loop."""
    (tmp_path / "wt").mkdir(exist_ok=True)
    outputs = iter(["fail 1", "fail 2", "pass"])
    engine = FakeEngine(
        {
            "coder": [FakeReply(tool_calls=[ToolCall("c", "shell", json.dumps({"cmd": "echo x"}))])] * 3
            + [FakeReply(text="done")]
        }
    )
    team, ctx = make_team(tmp_path, engine)

    class VaryingShell:
        name = "shell"
        description = "x"
        parameters = {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}

        async def run(self, args, c):
            from airlie.tools import ToolResult

            return ToolResult(True, next(outputs))

    ctx.tools.add(VaryingShell())
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.steps == 3
