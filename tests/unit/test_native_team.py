"""Native ReAct team loop over the Engine seam: tool calls become steps, observers see (u_t, o_t)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.config import RoleSpec
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.engine.types import Message
from airlie.prompts import entry, text
from airlie.scheduler import SlotScheduler
from airlie.supervisor.advice import SUPERVISOR
from airlie.teams import NativeTeam, StepObserver, StepRecord, TeamContext
from airlie.teams.native_team import (
    _without_stale_budget,
    dump_messages,
    is_test_path,
    load_messages,
)
from airlie.teams.protocol import StuckAdvisor
from airlie.tools import ToolContext, ToolRegistry
from tests.unit.advisor_doubles import RaisingAdvisor, RecordingAdvisor, SettleRaisingAdvisor


class Recorder(StepObserver):
    def __init__(self, inject: bool = False) -> None:
        self.steps: list[StepRecord] = []
        self.injections: list[tuple[str, int]] = []
        self.inject = inject
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def before_step(self, team_id: str, step_num: int) -> str | None:
        self.injections.append((team_id, step_num))
        return (
            "[Shared Memory Keys Available]:\n- Key [m1]: nothing yet"
            if self.inject and step_num == 2
            else None
        )

    async def after_step(self, step: StepRecord) -> None:
        self.steps.append(step)

    async def emit(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))


def make_team(
    tmp_path: Path,
    engine: FakeEngine,
    *,
    max_steps: int = 10,
    token_budget: int = 10**6,
    role_name: str = "coder",
    tool_names: tuple[str, ...] = ("write_file", "read_file", "run_tests"),
    **role_fields: Any,
):
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    (tmp_path / "coder.md").write_text("You are the coder.")
    fields = {"thinking": False, "max_tokens": 64, **role_fields}
    role = RoleSpec(prompt=tmp_path / "coder.md", model="base", **fields)
    tools = ToolRegistry.builtin(list(tool_names), ToolContext(worktree=wt, test_cmd="echo ok"))
    ctx = TeamContext(
        team_id="team-1",
        goal="make it pass",
        role=role,
        tools=tools,
        worktree=wt,
        role_name=role_name,
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
    # the fourth request offered no tools and asked for the answer; a tool call there is told once, and a
    # second one ends the run
    assert not result.ok and result.stop_reason == "max_steps"
    assert result.steps == 3
    assert len(engine.requests) == 5 and engine.requests[-1].tools is None


async def test_the_last_step_gets_no_tools_and_its_answer_finishes_the_run(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))]),
                FakeReply(tool_calls=[ToolCall("c2", "read_file", json.dumps({"path": "b"}))]),
                FakeReply(text="The story, from what I read."),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine, max_steps=2)
    result = await team.run(ctx, Recorder())
    assert result.ok and result.stop_reason == "done"
    assert result.final_text == "The story, from what I read."
    assert result.steps == 2
    assert [r.tools is None for r in engine.requests] == [False, False, True]
    last = engine.requests[-1].messages[-1]
    assert last.role == "user" and "no more tools" in str(last.content).lower()


async def test_a_cut_off_last_answer_is_not_a_finished_one(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))]),
                FakeReply(text="The story so", finish_reason="length"),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine, max_steps=1)
    result = await team.run(ctx, Recorder())
    assert not result.ok and result.stop_reason == "cut_off"


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


# ---- a reply cut off at max_tokens is not a finished answer (HDS-3, 2026-10-15) ----

HDS_3 = {"thinking": True, "thinking_budget": 8192, "max_tokens": 4096}
"""The architect of the first live story: its thinking could use the whole of max_tokens."""


async def test_a_reply_cut_off_with_no_visible_text_ends_the_turn_unsuccessfully_and_says_why(
    tmp_path: Path,
) -> None:
    """The architect read six files and then thought until max_tokens. The server split the thinking off, so
    the text was empty; the loop took that for its final answer and the story failed with "no JSON object"."""
    cut = FakeReply(reasoning="Let me look at the layers again. ", finish_reason="length")
    engine = FakeEngine({"architect": [cut]})
    team, ctx = make_team(tmp_path, engine, role_name="architect", **HDS_3)
    result = await team.run(ctx, Recorder())
    assert result.ok is False and result.stop_reason == "cut_off"
    assert result.final_text == (
        "the reply was cut off at max_tokens (4096) before it ended, still thinking; "
        "raise roles.architect.max_tokens above its thinking_budget (8192)"
    )
    assert result.steps == 0 and len(engine.requests) == 1  # it is not asked again
    assert result.completion_tokens == 7  # the tokens it spent are counted


async def test_a_reply_cut_off_after_tool_steps_keeps_the_steps_it_took(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a.py"}))]),
                FakeReply(reasoning="Now the second file. ", finish_reason="length"),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    rec = Recorder()
    result = await team.run(ctx, rec)
    assert result.stop_reason == "cut_off" and not result.ok and result.steps == 1
    assert [s.u_t.name for s in rec.steps] == ["read_file"]


async def test_a_cut_off_reply_that_has_text_keeps_it_and_is_marked_cut_off(tmp_path: Path) -> None:
    cut = FakeReply(text="The plan has three lay", reasoning="Two layers? Three. ", finish_reason="length")
    team, ctx = make_team(tmp_path, FakeEngine({"coder": [cut]}))
    result = await team.run(ctx, Recorder())
    assert result.final_text == "The plan has three lay"  # its text, not a message about the cut-off
    assert result.stop_reason == "cut_off" and result.ok is False


async def test_a_cut_off_reply_whose_text_is_only_whitespace_has_no_visible_text(tmp_path: Path) -> None:
    cut = FakeReply(text=" \n\t ", reasoning="hmm ", finish_reason="length")
    team, ctx = make_team(tmp_path, FakeEngine({"coder": [cut]}))
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "cut_off"
    assert result.final_text == (
        "the reply was cut off at max_tokens (64) before it ended, still thinking; "
        "raise roles.coder.max_tokens"
    )


@pytest.mark.parametrize(
    ("fields", "reasoning", "expected"),
    [
        pytest.param(
            {"thinking": True, "thinking_budget": 4096, "max_tokens": 4096},
            "hmm",
            "the reply was cut off at max_tokens (4096) before it ended, still thinking; "
            "raise roles.coder.max_tokens above its thinking_budget (4096)",
            id="the budget is as big as max_tokens: it leaves no room",
        ),
        pytest.param(
            {"thinking": True, "thinking_budget": 4095, "max_tokens": 4096},
            "hmm",
            "the reply was cut off at max_tokens (4096) before it ended, still thinking; "
            "raise roles.coder.max_tokens",
            id="a budget below max_tokens is not what to raise it above",
        ),
        pytest.param(
            {"thinking": True, "thinking_budget": None, "max_tokens": 4096},
            "hmm",
            "the reply was cut off at max_tokens (4096) before it ended, still thinking; "
            "raise roles.coder.max_tokens",
            id="uncapped thinking has no budget to name",
        ),
        pytest.param(
            {"thinking": False, "thinking_budget": 8192, "max_tokens": 4096},
            "",
            "the reply was cut off at max_tokens (4096) before it ended; raise roles.coder.max_tokens",
            id="a role that does not think: no thinking, no budget",
        ),
    ],
)
async def test_the_cut_off_message_names_what_to_raise(
    tmp_path: Path, fields: dict[str, Any], reasoning: str, expected: str
) -> None:
    cut = FakeReply(reasoning=reasoning, finish_reason="length")
    team, ctx = make_team(tmp_path, FakeEngine({"coder": [cut]}), **fields)
    assert (await team.run(ctx, Recorder())).final_text == expected


async def test_a_tool_call_is_unaffected_by_a_length_finish(tmp_path: Path) -> None:
    """Only a reply with no tool call is judged by how it ended; a call is run as ever."""
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(
                    tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a.py"}))],
                    finish_reason="length",
                ),
                FakeReply(text="Read it."),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    result = await team.run(ctx, Recorder())
    assert result.ok and result.stop_reason == "done" and result.steps == 1
    assert result.final_text == "Read it."


async def test_a_reply_that_stopped_by_itself_is_done_whatever_it_holds(tmp_path: Path) -> None:
    only_thinking = FakeReply(reasoning="All of it was thinking. ", finish_reason="stop")
    team, ctx = make_team(tmp_path, FakeEngine({"coder": [only_thinking]}))
    result = await team.run(ctx, Recorder())
    assert result.ok and result.stop_reason == "done" and result.final_text == ""


# ---- fork-when-stuck seam: stop predicate, state snapshot, resume with nudge, preamble ----


async def test_stop_when_returns_fork_point_with_state_and_last_test_output(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(tool_calls=[ToolCall("c1", "run_tests", "{}")]),
                FakeReply(tool_calls=[ToolCall("c2", "run_tests", "{}")]),
                FakeReply(text="never reached"),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    ctx.tools = ToolRegistry.builtin(
        ["run_tests"], ToolContext(worktree=ctx.worktree, test_cmd="echo FAILED x; false")
    )
    seen: list[str] = []

    def stuck(step: StepRecord) -> bool:
        seen.append(step.u_t.name)
        return len(seen) >= 2

    ctx.stop_when = stuck
    res = await team.run(ctx, Recorder())
    assert res.stop_reason == "fork_point" and res.steps == 2
    assert res.state is not None and len(res.state["messages"]) == 6  # system, goal, 2×(assistant, tool)
    assert "FAILED x" in res.last_test_output
    assert len(engine.requests) == 2  # the third reply was never requested


async def test_initial_state_resumes_verbatim_and_nudge_is_next_user_message(tmp_path: Path) -> None:
    from airlie.engine.types import Message
    from airlie.teams.native_team import dump_messages

    history = [
        Message(role="system", content="You are the coder."),
        Message(role="user", content="Goal:\nmake it pass"),
        Message(role="assistant", content="", tool_calls=(ToolCall("c1", "run_tests", "{}"),)),
        Message(role="tool", content="FAILED", tool_call_id="c1", name="run_tests"),
    ]
    engine = FakeEngine({"coder": [FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.initial_state = {"messages": dump_messages(history)}
    ctx.nudge = "Branch r1-1 of 2. Try a different approach."
    res = await team.run(ctx, Recorder())
    assert res.ok and res.stop_reason == "done"
    sent = engine.requests[0].messages
    assert [m.role for m in sent] == ["system", "user", "assistant", "tool", "user"]
    assert sent[2].tool_calls == (ToolCall("c1", "run_tests", "{}"),)
    assert sent[-1].content == ctx.nudge


async def test_preamble_precedes_goal(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.preamble = "[Repository map]\ntests/\n  test_a.py"
    await team.run(ctx, Recorder())
    goal_msg = engine.requests[0].messages[1].content
    assert goal_msg.startswith("[Repository map]") and "Goal:\nmake it pass" in goal_msg


# ---- step timing: prefill (ttft) split from generation, on the StepRecord the observer sees ----


class TtftEngine(FakeEngine):
    """A fake engine whose reply took 800 ms to first token: the split the metrics tab charts."""

    async def generate(self, req, on_token=None):
        res = await super().generate(req, on_token)
        res.ttft_ms = 800.0
        return res


class FakeClock:
    """A scripted monotonic clock: each call returns the current time, then advances by the next delta."""

    def __init__(self, deltas_ms: list[float]) -> None:
        self._deltas = list(deltas_ms)
        self.t = 0.0

    def monotonic(self) -> float:
        t = self.t
        if self._deltas:
            self.t += self._deltas.pop(0) / 1000.0
        return t


def _write_then_done() -> FakeEngine:
    return TtftEngine(
        {
            "coder": [
                FakeReply(
                    tool_calls=[
                        ToolCall("c1", "write_file", json.dumps({"path": "a.py", "content": "x=1\n"}))
                    ]
                ),
                FakeReply(text="done"),
            ]
        }
    )


async def test_step_record_splits_prefill_from_generation(tmp_path: Path, monkeypatch) -> None:
    """A refine-shaped step: 1300 ms of engine time, 800 ms of it prefill (ttft) -> gen_ms is the 500 ms
    of generation only, and prefill_ms the 800."""
    team, ctx = make_team(tmp_path, _write_then_done())
    # the clock native_team reads: run start, then the 1300 ms request, the 12 ms tool, the final turn
    monkeypatch.setattr("airlie.teams.native_team.time", FakeClock([0, 1300, 0, 12, 0, 0, 0, 0]))
    rec = Recorder()
    result = await team.run(ctx, rec)
    assert result.ok and result.steps == 1
    step = rec.steps[0]
    assert step.prefill_ms == 800.0
    assert step.gen_ms == 500.0  # the 1300 ms request minus its 800 ms prefill
    assert step.gen_ms < step.prefill_ms  # a refine step: large prompt, small completion


async def test_step_record_prefill_is_the_result_ttft_and_gen_never_negative(
    tmp_path: Path, monkeypatch
) -> None:
    """When the engine reports a ttft longer than the wall time around it (a slow client), generation is
    zero, not negative."""
    team, ctx = make_team(tmp_path, _write_then_done())
    monkeypatch.setattr("airlie.teams.native_team.time", FakeClock([0, 100, 0, 12, 0, 0, 0, 0]))
    rec = Recorder()
    await team.run(ctx, rec)
    step = rec.steps[0]
    assert step.prefill_ms == 800.0
    assert step.gen_ms == 0.0


async def test_only_the_first_tool_call_of_a_reply_is_executed(tmp_path: Path) -> None:
    """Parity with the AutoGen path (2026-10-13): one action per step; later calls in a 'plan' are often
    argument-less, and the history must only carry the call that ran."""
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(
                    tool_calls=[
                        ToolCall("c1", "write_file", json.dumps({"path": "a.py", "content": "x=1\\n"})),
                        ToolCall("c2", "read_file", "{}"),
                    ]
                ),
                FakeReply(text="done"),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    rec = Recorder()
    res = await team.run(ctx, rec)
    assert res.steps == 1 and [s.u_t.name for s in rec.steps] == ["write_file"]
    sent = engine.requests[1].messages
    assert sent[2].tool_calls == (
        ToolCall("c1", "write_file", json.dumps({"path": "a.py", "content": "x=1\\n"})),
    )
    assert [m.role for m in sent] == ["system", "user", "assistant", "tool"]


async def test_a_call_whose_arguments_do_not_parse_is_replayed_without_them(tmp_path: Path) -> None:
    runaway = '{"path": "a.py", "content": "def f():\\n    return compute(1, 2, 3) if'
    engine = FakeEngine(
        {"coder": [FakeReply(tool_calls=[ToolCall("c1", "write_file", runaway)]), FakeReply(text="done")]}
    )
    team, ctx = make_team(tmp_path, engine)
    rec = Recorder()
    await team.run(ctx, rec)
    assert rec.steps[0].u_t.arguments == runaway  # the trace keeps what the model sent
    assert "were cut off" in rec.steps[0].o_t  # and the model is told why nothing happened
    assert engine.requests[1].messages[2].tool_calls == (ToolCall("c1", "write_file", "{}"),)


def test_dump_and_load_messages_keep_parts() -> None:
    from airlie.engine import ContentPart, Message
    from airlie.teams.native_team import dump_messages, load_messages

    m = Message(
        role="user", content="see image", parts=(ContentPart("image_url", url="data:image/png;base64,AA=="),)
    )
    assert load_messages(dump_messages([m])) == [m]


async def test_prompt_frontmatter_is_not_sent_as_system_text(tmp_path: Path) -> None:
    from airlie.engine import FakeEngine, FakeReply

    engine = FakeEngine({"coder": [FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.role.prompt.write_text("---\nsummary: Implement code\n---\n# coder\n")
    await team.run(ctx, Recorder())
    assert engine.requests[0].messages[0].content == "# coder\n"


async def test_images_become_parts_on_the_goal_message(tmp_path: Path) -> None:
    from airlie.engine import FakeEngine, FakeReply

    png = tmp_path / "s.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    engine = FakeEngine({"coder": [FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.images = [png]
    await team.run(ctx, Recorder())
    goal = engine.requests[0].messages[1]
    assert goal.parts is not None and goal.parts[0].type == "text" and goal.parts[1].type == "image_url"


GARBLED = (
    "Let me look at the schema.\n\n<function=read_file>\n"
    "<file>src/airlie/config/schema.py</file>\n</function>"
)
"""HDS-5 layer 01 (2026-10-16): the coder's tool call as text the server could not parse."""
UNREPAIRABLE = "Let me look.\n\n<function=open_file>\n<file>src/airlie/config/schema.py</file>\n</function>"
"""The same shape naming a tool the role does not have: nothing to repair it into."""


async def test_a_tool_call_left_as_text_is_not_an_answer_and_gets_one_correction(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(text=UNREPAIRABLE),
                FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))]),
                FakeReply(text="Read it; nothing to change."),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    result = await team.run(ctx, Recorder())
    assert result.ok and result.stop_reason == "done"
    assert result.final_text == "Read it; nothing to change."
    assert result.steps == 1  # the correction is a turn, not a step
    asked = engine.requests[1].messages[-1]
    assert asked.role == "user" and "could not be run" in str(asked.content)


async def test_a_second_unparsed_tool_call_in_a_row_fails_the_run(tmp_path: Path) -> None:
    engine = FakeEngine(
        {"coder": [FakeReply(text=UNREPAIRABLE), FakeReply(text='<tool_call>\n{"name": "read_file"')]}
    )
    team, ctx = make_team(tmp_path, engine)
    result = await team.run(ctx, Recorder())
    assert not result.ok and result.stop_reason == "malformed_call"
    assert "<function=read_file>" not in result.final_text and "could not be parsed" in result.final_text
    assert len(engine.requests) == 2


async def test_each_streak_of_unparsed_calls_gets_its_own_correction(tmp_path: Path) -> None:
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine(
        {"coder": [FakeReply(text=UNREPAIRABLE), read, FakeReply(text=UNREPAIRABLE), FakeReply(text="Done.")]}
    )
    team, ctx = make_team(tmp_path, engine)
    result = await team.run(ctx, Recorder())
    assert result.ok and result.final_text == "Done."


async def test_on_the_last_turn_an_unparsed_tool_call_is_no_answer(tmp_path: Path) -> None:
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [read, FakeReply(text=UNREPAIRABLE), FakeReply(text=UNREPAIRABLE)]})
    team, ctx = make_team(tmp_path, engine, max_steps=1)
    result = await team.run(ctx, Recorder())
    assert not result.ok and result.stop_reason == "max_steps"


async def test_a_role_is_told_it_has_one_step_left_after_its_second_to_last(tmp_path: Path) -> None:
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [read, read, FakeReply(text="Final.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=3)
    result = await team.run(ctx, Recorder())
    assert result.ok and result.final_text == "Final."
    told = [
        [m for m in r.messages if m.role == "user" and "one step left" in str(m.content)]
        for r in engine.requests
    ]
    assert [len(t) for t in told] == [0, 0, 1]  # only the request after step 2 of 3 carries it


async def test_a_one_step_role_is_not_told_it_has_a_step_left(tmp_path: Path) -> None:
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [read, FakeReply(text="Final.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=1)
    await team.run(ctx, Recorder())
    assert not any("one step left" in str(m.content) for r in engine.requests for m in r.messages)


async def test_the_turn_after_the_last_step_thinks_unless_the_role_says_not_to(tmp_path: Path) -> None:
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])

    async def thinking_per_request(last_turn_thinking: bool) -> list[bool]:
        engine = FakeEngine({"coder": [read, read, FakeReply(text="Plan.")]})
        team, ctx = make_team(tmp_path, engine, max_steps=2)
        ctx.role = ctx.role.model_copy(update={"thinking": True, "last_turn_thinking": last_turn_thinking})
        await team.run(ctx, Recorder())
        return [r.thinking for r in engine.requests]

    assert await thinking_per_request(True) == [True, True, True]
    assert await thinking_per_request(False) == [True, True, False]  # only the tool-less last turn


async def test_a_tool_call_on_a_last_turn_without_thinking_is_retried_with_thinking(tmp_path: Path) -> None:
    """AIR-30 refine, 2026-10-19: the architect (thinking, last_turn_thinking false) answered its tool-less
    last turn with a tool call, twice, in both the survey and the layer plan, so the story blocked. The cheap
    turn is tried first; the one retry thinks, since a plan written from 30k tokens of reading is the hard
    part the turn exists for."""
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [read, read, read, FakeReply(text="The plan.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=2)
    ctx.role = ctx.role.model_copy(update={"thinking": True, "last_turn_thinking": False})
    result = await team.run(ctx, Recorder())
    assert result.ok and result.final_text == "The plan."
    assert [r.thinking for r in engine.requests] == [True, True, False, True]  # the retry thinks


async def test_a_tool_call_on_the_last_turn_is_told_once_and_the_answer_after_it_counts(
    tmp_path: Path,
) -> None:
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [read, read, FakeReply(text="The plan.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=1)
    result = await team.run(ctx, Recorder())
    assert result.ok and result.stop_reason == "done" and result.final_text == "The plan."
    assert result.steps == 1 and len(engine.requests) == 3
    assert engine.requests[-1].tools is None
    told = engine.requests[-1].messages[-1]
    assert told.role == "user" and "No tools are available on this turn" in str(told.content)


async def test_a_tool_call_on_the_last_turn_is_an_event_each_time(tmp_path: Path) -> None:
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [read, read, read]})
    team, ctx = make_team(tmp_path, engine, max_steps=1)
    events: list[tuple[str, dict[str, Any]]] = []
    ctx.emit = lambda kind, **f: events.append((kind, f))
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "max_steps"
    called = [f for k, f in events if k == "last_turn_tool_call"]
    assert called == [
        {"team_id": ctx.team_id, "role": ctx.role_name, "tool": "read_file", "ends_run": False},
        {"team_id": ctx.team_id, "role": ctx.role_name, "tool": "read_file", "ends_run": True},
    ]


async def test_a_role_without_thinking_does_not_think_on_the_last_turn_retry(tmp_path: Path) -> None:
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [read, read, FakeReply(text="The plan.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=1)
    ctx.role = ctx.role.model_copy(update={"thinking": False, "last_turn_thinking": False})
    await team.run(ctx, Recorder())
    assert [r.thinking for r in engine.requests] == [False, False, False]


def reads(n: int) -> list[FakeReply]:
    return [
        FakeReply(tool_calls=[ToolCall(f"r{i}", "read_file", json.dumps({"path": f"f{i}"}))])
        for i in range(n)
    ]


def told_to_write(engine: FakeEngine) -> list[int]:
    """The 1-based requests whose messages hold the write-now nudge."""
    return [
        i
        for i, r in enumerate(engine.requests, 1)
        if any(m.role == "user" and "written nothing. Stop reading" in str(m.content) for m in r.messages)
    ]


async def test_a_role_that_can_write_and_has_only_read_is_told_once_to_write_two_fifths_in(
    tmp_path: Path,
) -> None:
    engine = FakeEngine({"coder": [*reads(8), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10)
    await team.run(ctx, Recorder())
    # 10 steps: the fourth is the line; the fifth request is the first to carry it, and it carries it on
    assert told_to_write(engine)[0] == 5 and told_to_write(engine) == list(range(5, len(engine.requests) + 1))
    asked = [m for m in engine.requests[-1].messages if "written nothing. Stop reading" in str(m.content)]
    assert len(asked) == 1 and "4 of your 10 steps" in str(asked[0].content)  # said once, not each step


async def test_a_role_that_still_has_not_written_four_steps_after_the_nudge_has_its_reads_refused(
    tmp_path: Path,
) -> None:
    """AIR-28: the schemas offered never change (a changed tool list is a prompt the engine's prefix cache has
    never seen), so write-only is enforced where the call runs: a read is refused with an observation."""
    engine = FakeEngine({"coder": [*reads(12), FakeReply(text="Done.")]})
    team, ctx = make_team(
        tmp_path, engine, max_steps=12
    )  # nudge at step 4 (12 * 2 // 5 = 4), reading off at 8
    ctx.emit = events = Events()
    rec = Recorder()
    await team.run(ctx, rec)
    offered = [{t.name for t in (r.tools or [])} for r in engine.requests]
    assert all(
        o == {"write_file", "read_file", "run_tests"} for o in offered[:-1]
    )  # the same list every turn
    told = [
        i
        for i, r in enumerate(engine.requests, 1)
        if any("reading is switched off" in str(m.content) for m in r.messages)
    ]
    assert told[0] == 9 and told == list(range(9, len(engine.requests) + 1))
    # the hold still serves READS_UNDER_HOLD reads (steps 9-11), each saying how many are left; the fourth
    # (step 12) is refused: the reads of f0.. find no file (ok=False either way); the refusal is the
    # observation
    assert [
        s.o_t.endswith("\n\n(2 read(s) left while reading is switched off; then write)") for s in rec.steps
    ][8]
    assert [
        s.o_t.endswith("\n\n(0 read(s) left while reading is switched off; then write)") for s in rec.steps
    ][10]
    assert [
        s.o_t == "reading is switched off: use write_file, edit_file or run_tests" for s in rec.steps
    ] == [False] * 11 + [True]
    refused = [e for e in events.records if e["kind"] == "read_refused"]
    assert [(e["team_id"], e["role"], e["step"], e["tool"]) for e in refused] == [
        ("team-1", "coder", 12, "read_file")
    ]


async def test_a_role_that_writes_after_the_nudge_keeps_its_read_tools(tmp_path: Path) -> None:
    write = FakeReply(
        tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))]
    )
    engine = FakeEngine({"coder": [*reads(4), write, *reads(5), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=14)
    await team.run(ctx, Recorder())
    assert all("read_file" in {t.name for t in (r.tools or [])} for r in engine.requests[:-1])


async def test_a_writer_with_nudge_stuck_that_repeats_again_loses_the_tool_then_its_run(
    tmp_path: Path,
) -> None:
    """AIR-7 layer 02 attempts 4-6, 2026-10-18: every fix round ended as a repeat while probing, its change
    unmade. Told once; then the tool it loops on is switched off until it writes; a streak of refusals ends
    the run."""
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [same] * 12})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    rec = Recorder()
    result = await team.run(ctx, rec)
    # 3 repeats, told; 3 more, read_file switched off; 3 refusals, stopped
    assert result.stop_reason == "repeated_action" and result.steps == 9
    assert requests_saying(engine, "same call 3 times in a row")[0] == 4
    assert requests_saying(engine, "same call again after being told (read_file)")[0] == 7
    assert [s.o_t == REFUSED for s in rec.steps] == [False] * 6 + [True] * 3


async def run_repeating_writer(tmp_path: Path, advisor: StuckAdvisor | None) -> tuple[Any, FakeEngine]:
    """The writer of the test above (told, its tool switched off, stopped), with `advisor` as its on_stuck."""
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [same] * 12})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    ctx.on_stuck = advisor
    return await team.run(ctx, Recorder()), engine


def user_said(engine: FakeEngine) -> list[str]:
    """Every user message of the last request, in order: the goal and each nudge."""
    return [str(m.content) for m in engine.requests[-1].messages if m.role == "user"]


async def test_the_repeat_nudge_carries_the_advisors_section_once(tmp_path: Path) -> None:
    adv = RecordingAdvisor("Stop reading a; write the test.")
    result, engine = await run_repeating_writer(tmp_path, adv)
    assert result.stop_reason == "repeated_action" and result.steps == 9  # the guard itself is unchanged
    fixed = text("repeated", n=3, tool="read_file")
    said = user_said(engine)
    [nudge] = [m for m in said if m.startswith(fixed)]
    assert nudge == f"{fixed}\n\n{SUPERVISOR}\nStop reading a; write the test."
    assert text("repeat-write-only", tool="read_file") in said  # the hold is sent as it is, no advice
    assert sum(SUPERVISOR in m for m in said) == 1
    # asked once, with the subtask, the role, the fixed text and the conversation so far (system, goal, 3x2)
    assert adv.stuck_calls == [("team-1", "coder", fixed, 8)]
    # it repeated again (repeat-write-only): settled not helped then, and not again when the run stopped
    assert adv.stuck_settles == [("team-1", False)]


async def test_a_role_that_goes_on_after_the_advised_nudge_settles_it_with_its_result(tmp_path: Path) -> None:
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [same, same, same, *reads(1), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    adv = RecordingAdvisor("Read something else.")
    ctx.on_stuck = adv
    result = await team.run(ctx, Recorder())
    assert result.ok and adv.stuck_settles == [("team-1", True)]


async def test_an_advisor_with_nothing_to_say_leaves_the_fixed_nudge(tmp_path: Path) -> None:
    adv = RecordingAdvisor(None)
    _, engine = await run_repeating_writer(tmp_path, adv)
    assert text("repeated", n=3, tool="read_file") in user_said(engine)
    assert len(adv.stuck_calls) == 1


async def test_an_advisor_that_raises_leaves_the_fixed_nudge(tmp_path: Path) -> None:
    result, engine = await run_repeating_writer(tmp_path, RaisingAdvisor())
    assert result.stop_reason == "repeated_action" and result.steps == 9
    said = user_said(engine)
    assert text("repeated", n=3, tool="read_file") in said and not any(SUPERVISOR in m for m in said)


async def test_an_advisor_that_cannot_record_the_outcome_leaves_the_run_as_it_was(tmp_path: Path) -> None:
    result, engine = await run_repeating_writer(tmp_path, SettleRaisingAdvisor("Write the test."))
    assert result.stop_reason == "repeated_action" and result.steps == 9
    assert sum(SUPERVISOR in m for m in user_said(engine)) == 1


async def test_a_run_that_never_repeats_never_asks_or_settles(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [*reads(2), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    adv = RecordingAdvisor("unused")
    ctx.on_stuck = adv
    assert (await team.run(ctx, Recorder())).ok
    assert adv.stuck_calls == [] and adv.stuck_settles == []


async def test_a_writer_whose_looping_tool_is_switched_off_writes_and_gets_it_back(tmp_path: Path) -> None:
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    (tmp_path / "wt").mkdir(exist_ok=True)
    (tmp_path / "wt" / "a").write_text("x\n")
    engine = FakeEngine({"coder": [*[same] * 6, write("w", "b.py"), *reads(1), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    rec = Recorder()
    result = await team.run(ctx, rec)
    assert result.ok and result.final_text == "Done." and result.steps == 8
    assert rec.steps[6].u_t.name == "write_file" and rec.steps[6].ok
    assert rec.steps[7].u_t.name == "read_file" and rec.steps[7].o_t != REFUSED  # the read is served again


async def test_a_role_without_write_tools_that_repeats_again_after_the_nudge_is_stopped(
    tmp_path: Path,
) -> None:
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [same] * 9})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True, tool_names=("read_file",))
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "repeated_action" and result.steps == 6  # 3 repeats, told, 3 more repeats
    told = requests_saying(engine, "same call 3 times in a row")
    assert told[0] == 4 and told == list(range(4, len(engine.requests) + 1))


async def test_a_role_without_nudge_stuck_is_stopped_at_the_first_repeat(tmp_path: Path) -> None:
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [same] * 9})
    team, ctx = make_team(tmp_path, engine, max_steps=20)
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "repeated_action" and result.steps == 3


async def test_a_role_that_can_recover_after_the_nudge_goes_on(tmp_path: Path) -> None:
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [same, same, same, *reads(1), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    result = await team.run(ctx, Recorder())
    assert result.ok and result.final_text == "Done."


async def test_a_green_before_done_role_that_answers_with_run_tests_failing_is_told_once_to_go_on(
    tmp_path: Path,
) -> None:
    fail = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])
    engine = FakeEngine({"coder": [fail, FakeReply(text="Done."), FakeReply(text="Still done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10, green_before_done=True)
    # the registry's test command is `echo ok`: make it fail for this test (the context is frozen)
    object.__setattr__(ctx.tools.ctx, "test_cmd", "exit 1")
    result = await team.run(ctx, Recorder())
    assert result.ok and result.final_text == "Still done."  # told once, then its second answer stands
    told = engine.requests[-1].messages[-1]
    assert told.role == "user" and "last run_tests run failed" in str(told.content)


async def test_a_role_that_answers_green_or_without_the_setting_is_not_held_back(tmp_path: Path) -> None:
    passing = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])
    engine = FakeEngine({"coder": [passing, FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10, green_before_done=True)
    assert (await team.run(ctx, Recorder())).final_text == "Done."  # `echo ok` passes
    fail = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])
    engine = FakeEngine({"coder": [fail, FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10)  # no setting
    object.__setattr__(ctx.tools.ctx, "test_cmd", "exit 1")
    assert (await team.run(ctx, Recorder())).final_text == "Done."


async def test_a_role_that_has_written_is_not_told_to(tmp_path: Path) -> None:
    write = FakeReply(
        tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))]
    )
    engine = FakeEngine({"coder": [reads(1)[0], write, *reads(6), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10)
    await team.run(ctx, Recorder())
    assert told_to_write(engine) == []


def told_to_write_again(engine: FakeEngine) -> list[int]:
    """The 1-based requests whose messages hold the write-now-continuing nudge."""
    return [
        i
        for i, r in enumerate(engine.requests, 1)
        if any(m.role == "user" and "changed nothing yet" in str(m.content) for m in r.messages)
    ]


async def test_a_continuation_that_only_reads_is_nudged_then_held(tmp_path: Path) -> None:
    """AIR-9 layer 05, 2026-10-23: the tester's retry read for all 20 steps and never wrote."""
    engine = FakeEngine({"coder": [*reads(19), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20)
    ctx.already_written = ("tests/test_x.py",)
    ctx.emit = events = Events()
    rec = Recorder()
    await team.run(ctx, rec)
    assert told_to_write(engine) == []  # the first-run wording is not used
    assert told_to_write_again(engine)[0] == 9  # after step 8 (20 * 2 // 5)
    asked = [m for m in engine.requests[-1].messages if "changed nothing yet" in str(m.content)]
    assert len(asked) == 1 and "8 of your 20 steps" in str(asked[0].content)
    held = [
        i
        for i, r in enumerate(engine.requests, 1)
        if any("reading is switched off" in str(m.content) for m in r.messages)
    ]
    assert held[0] == 13  # after step 12
    assert "nothing this round" in str(
        next(m.content for m in engine.requests[-1].messages if "reading is switched off" in str(m.content))
    )
    assert [e["id"] for e in events.records if e["kind"] == "steer"][:2] == [
        "write-now-continuing",
        "write-only",
    ]
    assert [e["step"] for e in events.records if e["kind"] == "read_refused"]  # reads run out under the hold


async def test_a_continuation_held_to_write_reads_again_once_its_write_lands(tmp_path: Path) -> None:
    """Two findings: the hold ends with the first fix, so the second can be read for."""
    write = FakeReply(
        tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))]
    )
    engine = FakeEngine({"coder": [*reads(15), write, *reads(2), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20)
    ctx.already_written = ("tests/test_x.py",)
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    assert any("reading is switched off" in str(m.content) for m in engine.requests[13].messages)
    assert not [e for e in events.records if e["kind"] == "read_refused"]  # reads after the write are served


async def test_a_continuation_that_writes_first_is_not_nudged(tmp_path: Path) -> None:
    write = FakeReply(
        tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))]
    )
    engine = FakeEngine({"coder": [*reads(2), write, *reads(15), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20)
    ctx.already_written = ("tests/test_x.py",)
    await team.run(ctx, Recorder())
    assert told_to_write_again(engine) == [] and told_to_write(engine) == []
    assert not any("reading is switched off" in str(m.content) for r in engine.requests for m in r.messages)


async def test_a_continuation_without_write_tools_is_not_nudged(tmp_path: Path) -> None:
    engine = FakeEngine({"architect": [*reads(10), FakeReply(text="Done.")]})
    team, ctx = make_team(
        tmp_path, engine, max_steps=20, role_name="architect", tool_names=("read_file", "list_dir")
    )
    ctx.already_written = ("tests/test_x.py",)
    await team.run(ctx, Recorder())
    assert told_to_write_again(engine) == []


async def test_a_role_with_no_write_tool_is_not_told_to_write(tmp_path: Path) -> None:
    engine = FakeEngine({"architect": [*reads(8), FakeReply(text="Done.")]})
    team, ctx = make_team(
        tmp_path, engine, max_steps=10, role_name="architect", tool_names=("read_file", "list_dir")
    )
    await team.run(ctx, Recorder())
    assert told_to_write(engine) == []


class Events:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def __call__(self, kind: str, **fields: Any) -> None:
        self.records.append({"kind": kind, **fields})


async def test_a_call_in_a_known_misshape_is_repaired_and_run_as_a_step(tmp_path: Path) -> None:
    (tmp_path / "wt" / "src" / "airlie" / "config").mkdir(parents=True)
    (tmp_path / "wt" / "src" / "airlie" / "config" / "schema.py").write_text("SCHEMA = 1\n")
    engine = FakeEngine({"coder": [FakeReply(text=GARBLED), FakeReply(text="It defines SCHEMA.")]})
    team, ctx = make_team(tmp_path, engine)
    rec = Recorder()
    result = await team.run(ctx, rec)
    assert result.ok and result.final_text == "It defines SCHEMA." and result.steps == 1
    assert [s.u_t.name for s in rec.steps] == ["read_file"]
    assert json.loads(rec.steps[0].u_t.arguments) == {"path": "src/airlie/config/schema.py"}
    assert "SCHEMA = 1" in rec.steps[0].o_t
    history = engine.requests[1].messages
    assistant, tool = history[-2], history[-1]
    # replayed as a real call, prose apart
    assert assistant.content == "Let me look at the schema." and assistant.tool_calls
    assert tool.role == "tool" and tool.tool_call_id == assistant.tool_calls[0].id
    assert not any(m.role == "user" and "could not be run" in str(m.content) for m in history)


async def test_a_repaired_call_is_logged_as_a_malformed_reply_that_was_repaired(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [FakeReply(text=GARBLED), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    [ev] = [e for e in events.records if e["kind"] == "malformed_call"]
    assert ev["team_id"] == "team-1" and ev["role"] == "coder" and ev["step"] == 1
    assert ev["text"] == GARBLED
    assert ev["repair_tried"] is True and ev["repaired"] is True


async def test_an_unrepairable_reply_is_logged_with_its_text_and_why(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [FakeReply(text=UNREPAIRABLE), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    [ev] = [e for e in events.records if e["kind"] == "malformed_call"]
    assert ev["text"] == UNREPAIRABLE and ev["step"] == 1
    assert ev["repair_tried"] is True and ev["repaired"] is False and "open_file" in ev["repair_note"]


async def test_every_malformed_reply_is_logged_even_the_one_that_ends_the_run(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [FakeReply(text=UNREPAIRABLE), FakeReply(text=UNREPAIRABLE)]})
    team, ctx = make_team(tmp_path, engine)
    ctx.emit = events = Events()
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "malformed_call"
    assert len([e for e in events.records if e["kind"] == "malformed_call"]) == 2


async def test_a_long_malformed_reply_is_logged_capped(tmp_path: Path) -> None:
    long = UNREPAIRABLE + "x" * 20_000
    engine = FakeEngine({"coder": [FakeReply(text=long), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    [ev] = [e for e in events.records if e["kind"] == "malformed_call"]
    assert ev["text"] == long[:4000] and ev["chars"] == len(long)


async def test_on_the_last_turn_no_repair_is_tried(tmp_path: Path) -> None:
    read = FakeReply(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [read, FakeReply(text=GARBLED), FakeReply(text=GARBLED)]})
    team, ctx = make_team(tmp_path, engine, max_steps=1)
    ctx.emit = events = Events()
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "max_steps" and result.steps == 1
    evs = [e for e in events.records if e["kind"] == "malformed_call"]
    assert len(evs) == 2 and all(not e["repair_tried"] and not e["repaired"] for e in evs)


async def test_the_correction_shows_a_call_in_the_proper_format(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [FakeReply(text=UNREPAIRABLE), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine)
    await team.run(ctx, Recorder())
    asked = str(engine.requests[1].messages[-1].content)
    assert "<tool_call><function=read_file><parameter=path>" in asked


INVENTED = FakeReply(
    tool_calls=[ToolCall("c1", "Read", json.dumps({"file_path": "/home/luke/repos/x/README.md"}))]
)
"""2026-10-16, a design subtask with tools: []: it called tools from its training, none of which exist."""


async def test_a_role_without_tools_is_asked_to_answer_directly_and_offered_no_tools(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [FakeReply(text="Here is the design: …")]})
    team, ctx = make_team(tmp_path, engine, tool_names=())
    result = await team.run(ctx, Recorder())
    assert result.ok and result.final_text == "Here is the design: …" and result.steps == 0
    goal = str(engine.requests[0].messages[1].content)
    assert "answer" in goal.lower() and "Use the tools" not in goal and "run the tests" not in goal
    assert engine.requests[0].tools is None


async def test_a_role_whose_tools_only_read_is_not_told_to_change_files_or_run_tests(tmp_path: Path) -> None:
    """2026-10-29, bundle 2 run 3: the architect, told "change it, run the tests" with read tools only, argued
    with the brief till its thinking was cut at max_tokens."""
    engine = FakeEngine({"coder": [FakeReply(text="The plan.")]})
    team, ctx = make_team(tmp_path, engine, tool_names=("read_file", "list_dir", "grep"))
    await team.run(ctx, Recorder())
    goal = str(engine.requests[0].messages[1].content)
    assert "only read" in goal and "change it" not in goal and "run the tests" not in goal
    assert engine.requests[0].tools


async def test_a_role_without_tools_gets_one_correction_for_an_invented_call(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [INVENTED, FakeReply(text="The answer.")]})
    team, ctx = make_team(tmp_path, engine, tool_names=())
    rec = Recorder()
    result = await team.run(ctx, rec)
    assert result.ok and result.final_text == "The answer." and result.steps == 0
    assert rec.steps == []  # nothing was run
    asked = engine.requests[1].messages[-1]
    assert asked.role == "user" and "no tools" in str(asked.content).lower()


async def test_a_role_without_tools_that_keeps_calling_tools_stops(tmp_path: Path) -> None:
    text_call = FakeReply(text="<tool_call>\n<function=list_files>\n</function>\n</tool_call>")
    engine = FakeEngine({"coder": [INVENTED, text_call]})
    team, ctx = make_team(tmp_path, engine, tool_names=())
    result = await team.run(ctx, Recorder())
    assert not result.ok and result.stop_reason == "no_tools"
    assert len(engine.requests) == 2


async def test_a_step_records_what_the_model_thought(tmp_path: Path) -> None:
    """The reasoning of a thinking reply travels with the step (the trace keeps it: test_step_trace)."""
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(
                    reasoning="a.py is the only file; write it",
                    tool_calls=[
                        ToolCall("c1", "write_file", json.dumps({"path": "a.py", "content": "x=1\n"}))
                    ],
                ),
                FakeReply(text="Done."),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine, thinking=True, thinking_budget=16, max_tokens=64)
    rec = Recorder()
    await team.run(ctx, rec)
    assert [s.reasoning for s in rec.steps] == ["a.py is the only file; write it"]


# ---- the write schedule: a test write by one deadline, a code write by a later one ----

SCHEDULE = {"tests": 0.25, "code": 0.75}
REFUSED = "reading is switched off: use write_file, edit_file or run_tests"


def write(cid: str, path: str) -> FakeReply:
    return FakeReply(
        tool_calls=[ToolCall(cid, "write_file", json.dumps({"path": path, "content": "x = 1\n"}))]
    )


def offered_names(engine: FakeEngine) -> list[set[str]]:
    return [{t.name for t in (r.tools or [])} for r in engine.requests]


def requests_saying(engine: FakeEngine, text: str) -> list[int]:
    """The 1-based requests whose messages hold `text`."""
    return [
        i
        for i, r in enumerate(engine.requests, 1)
        if any(m.role == "user" and text in str(m.content) for m in r.messages)
    ]


def test_a_test_write_is_one_under_a_tests_directory_or_named_like_a_test() -> None:
    for p in (
        "tests/unit/test_a.py",
        "test/a.py",
        "pkg/tests/x.py",
        "test_a.py",
        "a_test.py",
        "a.test.ts",
        "a.spec.js",
        "tests/conftest.py",
        "src/conftest.py",
    ):
        assert is_test_path(p), p
    for p in ("src/airlie/teams/native_team.py", "a.py", "testing/notes.md", "src/contest.py", "latest.py"):
        assert not is_test_path(p), p


async def test_a_coder_that_only_reads_is_told_to_write_a_test_at_the_first_deadline_then_loses_reading(
    tmp_path: Path,
) -> None:
    engine = FakeEngine({"coder": [*reads(11), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=12, write_schedule=SCHEDULE)
    ctx.emit = events = Events()
    rec = Recorder()
    result = await team.run(ctx, rec)
    # 12 steps: the tests deadline is step 3 (a quarter), the code deadline step 9 (three quarters)
    told = requests_saying(engine, "written no test")
    assert told[0] == 4 and told == list(range(4, len(engine.requests) + 1))
    assert (
        requests_saying(engine, "written nothing. Stop reading") == []
    )  # the schedule replaces the old nudge
    off = requests_saying(engine, "reading is switched off")
    assert off[0] == 8  # four steps after the deadline, as before
    assert all(n == {"write_file", "read_file", "run_tests"} for n in offered_names(engine)[:-1])
    # from step 8 the hold serves three more reads (8, 9, 10), then refuses (11); the run ends on its text
    assert [s.o_t == REFUSED for s in rec.steps] == [False] * 10 + [True]
    assert result.stop_reason == "done" and result.steps == 11
    assert requests_saying(engine, "written no code")[0] == 10  # the code deadline, step 9, is missed too
    deadlines = [
        (e["kind_of_write"], e["step"], e["met"]) for e in events.records if e["kind"] == "write_deadline"
    ]
    assert deadlines == [("tests", 3, False), ("code", 9, False)]
    assert all(
        e["team_id"] == "team-1" and e["role"] == "coder"
        for e in events.records
        if e["kind"] == "write_deadline"
    )


async def test_a_coder_that_writes_its_test_and_code_in_time_is_never_nudged(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "coder": [
                *reads(1),
                write("t", "tests/test_a.py"),
                *reads(2),
                write("c", "a.py"),
                *reads(6),
                FakeReply(text="Done."),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine, max_steps=12, write_schedule=SCHEDULE)
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    assert (
        requests_saying(engine, "written no test") == [] and requests_saying(engine, "written no code") == []
    )
    assert all("read_file" in n for n in offered_names(engine)[:-1])
    deadlines = [
        (e["kind_of_write"], e["step"], e["met"]) for e in events.records if e["kind"] == "write_deadline"
    ]
    assert deadlines == [("tests", 3, True), ("code", 9, True)]


async def test_a_coder_that_wrote_only_tests_is_told_to_write_code_at_the_second_deadline(
    tmp_path: Path,
) -> None:
    engine = FakeEngine(
        {"coder": [*reads(1), write("t", "tests/test_a.py"), *reads(17), FakeReply(text="Done.")]}
    )
    team, ctx = make_team(tmp_path, engine, max_steps=20, write_schedule=SCHEDULE)  # deadlines 5 and 15
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    assert requests_saying(engine, "written no test") == []
    told = requests_saying(engine, "written no code")
    assert told[0] == 16 and "15 of your 20 steps" in str(engine.requests[15].messages[-1].content)
    assert requests_saying(engine, "reading is switched off")[0] == 20  # step 19, four after the deadline
    assert all(n == {"write_file", "read_file", "run_tests"} for n in offered_names(engine)[:-1])
    deadlines = [
        (e["kind_of_write"], e["step"], e["met"]) for e in events.records if e["kind"] == "write_deadline"
    ]
    assert deadlines == [("tests", 5, True), ("code", 15, False)]


async def test_reading_comes_back_once_the_missed_write_lands(tmp_path: Path) -> None:
    engine = FakeEngine(
        {"coder": [*reads(12), write("t", "tests/test_a.py"), *reads(2), FakeReply(text="Done.")]}
    )
    team, ctx = make_team(tmp_path, engine, max_steps=16, write_schedule=SCHEDULE)  # deadlines 4 and 12
    rec = Recorder()
    await team.run(ctx, rec)
    assert all(n == {"write_file", "read_file", "run_tests"} for n in offered_names(engine)[:-1])
    # reading off after the missed tests deadline (step 8): steps 9-11 are the hold's three reads, step 12
    # is refused; the test lands at step 13, and steps 14 and 15 read again with nothing appended
    assert [s.o_t == REFUSED for s in rec.steps][8:] == [False, False, False, True, False, False, False]
    assert "read(s) left" in rec.steps[8].o_t and "read(s) left" not in rec.steps[13].o_t


async def test_the_same_observation_three_times_is_a_repeat_whatever_the_arguments(tmp_path: Path) -> None:
    """AIR-7 2026-10-18: read_file of one file with offsets 438, 438 and 446 returned the identical cut text
    three times; the guard keyed on the arguments and let it go on."""
    calls = [
        FakeReply(tool_calls=[ToolCall(f"c{i}", "shell", json.dumps({"cmd": f"echo {i}"}))]) for i in range(3)
    ]
    engine = FakeEngine({"coder": [*calls, FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)

    class SameShell:
        name = "shell"
        description = "x"
        parameters = {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}

        async def run(self, args, c):
            from airlie.tools import ToolResult

            return ToolResult(True, "the same 20,000 chars")

    ctx.tools.add(SameShell())
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "repeated_action" and result.steps == 3


async def test_a_continuation_counts_the_files_already_written(tmp_path: Path) -> None:
    """AIR-7 2026-10-18: the fix round after a review started with blank flags, told the coder at step 10 it
    had "written no test" (its tests were in the worktree from the run before) and switched reading off."""
    engine = FakeEngine({"coder": [*reads(11), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=12, write_schedule=SCHEDULE)
    ctx.already_written = ("tests/unit/test_a.py",)
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    assert requests_saying(engine, "written no test") == []
    deadlines = [
        (e["kind_of_write"], e["step"], e["met"]) for e in events.records if e["kind"] == "write_deadline"
    ]
    assert deadlines == [("code", 3, False)]  # a change of its own is still owed, by the earlier deadline
    # and the one write-now nudge of a role without a schedule counts an earlier write too
    engine = FakeEngine({"coder": [*reads(8), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10)
    ctx.already_written = ("a.py",)
    await team.run(ctx, Recorder())
    assert told_to_write(engine) == []


async def test_a_continuation_owes_a_change_of_its_own_by_the_earlier_deadline(tmp_path: Path) -> None:
    """AIR-7 layer 02 attempt 5, 2026-10-18: the fix round's two deadlines were both met by the first run's
    files, so nothing pushed it to write; it probed for 28 steps and changed nothing. A continuation has one
    deadline, the earlier one, for a write of any kind this round, and loses its read tools WRITE_ONLY_AFTER
    steps later until that write lands."""
    engine = FakeEngine({"coder": [*reads(8), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=12, write_schedule=SCHEDULE)
    ctx.already_written = ("tests/unit/test_a.py", "a.py")
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    deadlines = [
        (e["kind_of_write"], e["step"], e["met"]) for e in events.records if e["kind"] == "write_deadline"
    ]
    assert deadlines == [("code", 3, False)]
    assert min(requests_saying(engine, "changed nothing")) == 4  # said after step 3
    assert min(requests_saying(engine, "written nothing this round")) == 8  # reading off after step 7
    assert (
        requests_saying(engine, "written no code") == [] and requests_saying(engine, "written no test") == []
    )
    # an edit of a test file is the change this round owes, as a code edit would be
    engine = FakeEngine(
        {"coder": [*reads(2), write("w", "tests/unit/test_b.py"), *reads(5), FakeReply(text="Done.")]}
    )
    team, ctx = make_team(tmp_path, engine, max_steps=12, write_schedule=SCHEDULE)
    ctx.already_written = ("tests/unit/test_a.py", "a.py")
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    met = [(e["step"], e["met"]) for e in events.records if e["kind"] == "write_deadline"]
    assert met == [(3, True)] and requests_saying(engine, "changed nothing") == []


async def test_three_edits_of_one_file_in_a_row_are_not_a_repeat(tmp_path: Path) -> None:
    """AIR-7 layer 02, 2026-10-18: a fix round edited src/airlie/metrics/__init__.py three times running, each
    answered "edited …", and the guard keyed on (tool, observation) stopped the loop as a repeat. A write's
    observation says nothing about what was written: for a write the arguments count too."""
    (tmp_path / "wt").mkdir(exist_ok=True)
    (tmp_path / "wt" / "a.py").write_text("x = 1\ny = 2\nz = 3\n")
    edits = [
        FakeReply(
            tool_calls=[ToolCall(f"e{i}", "edit_file", json.dumps({"path": "a.py", "old": o, "new": n}))]
        )
        for i, (o, n) in enumerate((("x = 1", "x = 10"), ("y = 2", "y = 20"), ("z = 3", "z = 30")))
    ]
    engine = FakeEngine({"coder": [*edits, FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, tool_names=("write_file", "edit_file", "read_file", "run_tests"))
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.steps == 3
    assert (tmp_path / "wt" / "a.py").read_text() == "x = 10\ny = 20\nz = 30\n"


async def test_a_writer_whose_first_reply_is_text_is_told_once_to_act(tmp_path: Path) -> None:
    """AIR-7 layer 02 fix round, 2026-10-18: the coder answered the reviewer's findings with 2,186 tokens of
    prose and no tool call, and the run ended as done with nothing changed."""
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(text="Here is how I would fix it: change _load to register a view, then…"),
                write("w", "a.py"),
                FakeReply(text="Done: a.py changed."),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    ctx.must_act = True
    ctx.emit = events = Events()
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.steps == 1
    told = requests_saying(engine, "nothing has been changed")
    assert told == [2, 3]  # said once, after the first reply; carried in the conversation after that
    assert engine.requests[1].messages[-2].content.startswith("Here is how I would fix it")  # its text kept
    assert [e["kind"] for e in events.records if e["kind"] == "text_before_action"] == ["text_before_action"]


async def test_a_writer_whose_reply_is_empty_is_told_once_to_act(tmp_path: Path) -> None:
    """AIR-9 layer 02 tester retry, 2026-10-21: sent back to add two missing imports, the tester thought for
    six steps of reads, then replied with nothing (no text, no call), and the run ended as done with the lint
    failure still there. An empty reply is no more an answer than prose is."""
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(reasoning="Let me reconsider the whole design of the test…"),
                write("w", "a.py"),
                FakeReply(text="Done: a.py changed."),
            ]
        }
    )
    team, ctx = make_team(tmp_path, engine)
    ctx.must_act = True
    ctx.emit = events = Events()
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.steps == 1 and result.final_text == "Done: a.py changed."
    assert requests_saying(engine, "nothing has been changed") == [2, 3]
    assert [e["chars"] for e in events.records if e["kind"] == "text_before_action"] == [0]


async def test_a_writer_that_answers_with_text_twice_is_done_as_before(tmp_path: Path) -> None:
    engine = FakeEngine(
        {"coder": [FakeReply(text="I would change a.py."), FakeReply(text="Still just words.")]}
    )
    team, ctx = make_team(tmp_path, engine)
    ctx.must_act = True
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.steps == 0 and result.final_text == "Still just words."


async def test_a_role_without_write_tools_may_answer_with_text_at_once(tmp_path: Path) -> None:
    engine = FakeEngine({"architect": [FakeReply(text="The plan: two layers.")]})
    team, ctx = make_team(tmp_path, engine, role_name="architect", tool_names=("read_file", "list_dir"))
    ctx.must_act = True
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.final_text == "The plan: two layers."
    assert requests_saying(engine, "nothing has been changed") == []


async def test_a_writer_whose_only_edit_failed_and_then_answers_with_text_is_sent_back_once(
    tmp_path: Path,
) -> None:
    """AIR-7 layer 03, 2026-10-18: a fix round's one edit missed its anchor, the next reply was prose, and the
    run ended as done with nothing changed. A write that failed is no change; a write that landed is."""
    (tmp_path / "wt").mkdir(exist_ok=True)
    (tmp_path / "wt" / "a.py").write_text("x = 1\n")
    miss = FakeReply(
        tool_calls=[ToolCall("e1", "edit_file", json.dumps({"path": "a.py", "old": "nope", "new": "y"}))]
    )
    engine = FakeEngine(
        {"coder": [miss, FakeReply(text="I give up."), write("w", "b.py"), FakeReply(text="Done.")]}
    )
    team, ctx = make_team(tmp_path, engine, tool_names=("write_file", "edit_file", "read_file", "run_tests"))
    ctx.must_act = True
    ctx.emit = events = Events()
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.steps == 2 and result.final_text == "Done."
    assert requests_saying(engine, "nothing has been changed")[0] == 3  # after the prose, not after the miss
    assert [e["kind"] for e in events.records if e["kind"] == "text_before_action"] == ["text_before_action"]


class FlakyEngine(FakeEngine):
    """A fake engine whose first `failures` generations raise EngineError, as a stalled request does."""

    def __init__(self, replies: Any, failures: int) -> None:
        super().__init__(replies)
        self.failures = failures
        self.errors = 0

    async def generate(self, req, on_token=None):  # type: ignore[no-untyped-def]
        from airlie.engine.types import EngineError

        if self.errors < self.failures:
            self.errors += 1
            raise EngineError("OUTPUT INACTIVITY TIMEOUT after 600.0s without a generator chunk")
        return await super().generate(req, on_token)


async def test_a_stalled_generation_is_asked_again_once_before_the_run_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AIR-31 layer 02, 2026-10-19: vllm-mlx sent one chunk and then nothing for its 600 s guard; the run
    ended as engine_error with its conversation lost. One retry of the same step, after a pause, and the
    run goes on; a second failure in a row still stops it."""
    from airlie.teams import native_team as nt

    monkeypatch.setattr(nt, "ENGINE_RETRY_S", 0.0)
    engine = FlakyEngine({"coder": [write("w", "a.py"), FakeReply(text="Done.")]}, failures=1)
    team, ctx = make_team(tmp_path, engine)
    ctx.emit = events = Events()
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.steps == 1 and engine.errors == 1
    retries = [e for e in events.records if e["kind"] == "engine_retry"]
    assert [(e["step"], e["attempt"]) for e in retries] == [(1, 1)]
    assert "INACTIVITY" in retries[0]["error"]
    engine = FlakyEngine({"coder": [write("w", "a.py"), FakeReply(text="Done.")]}, failures=2)
    team, ctx = make_team(tmp_path, engine)
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "engine_error" and engine.errors == 2


async def test_every_step_carries_the_contexts_attempt(tmp_path: Path) -> None:
    """The caller numbers the runs of a team (the studio: 0, then one per fix round or follow-up, whether
    the conversation resumes or starts again); the step records carry it, so the observer keeps each run's
    steps apart."""
    engine = FakeEngine({"coder": [write("w", "a.py"), FakeReply(text="done")] * 2})
    team, ctx = make_team(tmp_path, engine)
    rec = Recorder()
    await team.run(ctx, rec)
    assert rec.steps[-1].attempt == 0
    ctx.attempt = 2
    await team.run(ctx, rec)
    assert rec.steps[-1].attempt == 2


async def test_a_resumed_conversation_drops_the_last_runs_end_of_budget_messages(tmp_path: Path) -> None:
    """AIR-7 layer 02 fix round, 2026-10-18: the resumed history still said "You have used all 40 of your
    steps: no more tools" and "No tools are available on this turn", so the coder answered the reviewer's
    findings with "I'm out of steps, but here is the complete fix" and a code block instead of acting."""
    one_step_left, last_step, last_turn_retry = (
        text("one-step-left"),
        text("last-step", n=40),
        text("last-turn-retry"),
    )
    history = [
        Message(role="system", content="You are the coder."),
        Message(role="user", content="Goal:\nmake it pass"),
        Message(role="assistant", content="", tool_calls=(ToolCall("c1", "run_tests", "{}"),)),
        Message(role="tool", content="FAILED", tool_call_id="c1", name="run_tests"),
        Message(role="user", content=one_step_left),
        Message(role="assistant", content="", tool_calls=(ToolCall("c2", "read_file", '{"path": "a"}'),)),
        Message(role="tool", content="1: x", tool_call_id="c2", name="read_file"),
        Message(role="user", content=last_step),
        Message(role="assistant", content="I would fix it like this…"),
        Message(role="user", content=last_turn_retry),
        Message(role="assistant", content="Final answer: the fix is …"),
    ]
    engine = FakeEngine({"coder": [write("w", "a.py"), FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine, max_steps=12)
    ctx.initial_state = {"messages": dump_messages(history)}
    ctx.nudge = "A reviewer found blocking problems with your change. Fix each one, run the tests, then stop:"
    res = await team.run(ctx, Recorder())
    assert res.ok and res.steps == 1
    sent = engine.requests[0].messages
    texts = [str(m.content) for m in sent if m.role == "user"]
    assert one_step_left not in texts and last_step not in texts and last_turn_retry not in texts
    assert sent[-1].role == "user" and sent[-1].content.startswith(ctx.nudge)
    assert "12 steps" in str(sent[-1].content) and "every tool" in str(
        sent[-1].content
    )  # the budget is fresh
    assert [m.role for m in sent] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "assistant",
        "user",
    ]


# ---- a repeat is a repeat through run-to-run noise and round a cycle (AIR-9 layer 02, 2026-10-21) ----

PYTEST_RUN = (
    "{dots}  [100%]\n"
    "tmp_path = PosixPath('/private/var/T/pytest-of-noah/pytest-{n}/popen-gw{w}/test_x0')\n"
    "E   ValueError: too many values to unpack (expected 1)\n"
    "{failed} failed, 57 passed in 0.{n}s"
)


class ScriptedShell:
    """A shell tool answering each call with the next of `outputs`."""

    name = "shell"
    description = "x"
    parameters = {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = iter(outputs)

    async def run(self, args, c):
        from airlie.tools import ToolResult

        return ToolResult(False, next(self.outputs))


def shells(n: int) -> list[FakeReply]:
    return [
        FakeReply(tool_calls=[ToolCall(f"s{i}", "shell", json.dumps({"cmd": "pytest"}))]) for i in range(n)
    ]


async def test_the_same_test_run_three_times_is_a_repeat_whatever_its_timings_and_tmp_paths(
    tmp_path: Path,
) -> None:
    """AIR-9 layer 02: ten run_tests in a row with the one failure each time, told apart only by the run's
    duration, its tmp_path root, its xdist worker and the order of the progress dots."""
    runs = [
        PYTEST_RUN.format(dots=d, n=n, w=w, failed=1)
        for d, n, w in ((".F...", 52521, 6), ("...F.", 52522, 3), ("F....", 52523, 1))
    ]
    engine = FakeEngine({"coder": [*shells(3), FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.tools.add(ScriptedShell(runs))
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "repeated_action" and result.steps == 3


async def test_a_test_run_whose_failure_count_changes_is_not_a_repeat(tmp_path: Path) -> None:
    """The noise is blanked, not the result: a run that fails fewer tests each time is a fix loop."""
    runs = [PYTEST_RUN.format(dots=".F...", n=52521, w=6, failed=f) for f in (3, 2, 1)]
    engine = FakeEngine({"coder": [*shells(3), FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.tools.add(ScriptedShell(runs))
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.steps == 3


async def test_going_round_the_same_three_reads_three_times_is_a_repeat(tmp_path: Path) -> None:
    """AIR-9 layer 02: a follow-up read the same three files in turn for 16 steps; a guard that looked for one
    call repeated never fired. Without nudge_stuck the run stops after the third round."""
    engine = FakeEngine({"coder": [*reads(3) * 4, FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20)
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "repeated_action" and result.steps == 9
    assert result.final_text == "stopped: repeated the same action 3 times (read_file)"


async def test_a_writer_going_round_a_cycle_is_told_then_loses_its_reads(tmp_path: Path) -> None:
    """The cycle earns what one repeated call does: told once (cycled), then reading is switched off until
    it writes."""
    engine = FakeEngine({"coder": [*reads(2) * 6, FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    rec = Recorder()
    await team.run(ctx, rec)
    assert requests_saying(engine, "same 2 calls over and over, 3 times in a row")[0] == 7
    assert requests_saying(engine, "same call again after being told (read_file)")[0] == 13
    assert [s.o_t == REFUSED for s in rec.steps][:12] == [False] * 12


async def test_two_reads_in_turn_whose_results_change_are_not_a_repeat(tmp_path: Path) -> None:
    (tmp_path / "wt").mkdir(exist_ok=True)
    runs = [PYTEST_RUN.format(dots=".", n=1, w=1, failed=f) for f in (3, 2, 1)]
    calls = [r for pair in zip(reads(1) * 3, shells(3), strict=True) for r in pair]
    engine = FakeEngine({"coder": [*calls, FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    ctx.tools.add(ScriptedShell(runs))
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "done" and result.steps == 6


CUT = "Your reasoning on the last turn, cut at the budget:"
GO_ON = "Do not start over: continue from here and decide."


def thought(words: int, tag: str = "t") -> str:
    """Reasoning of `words` words (the FakeEngine counts a reply's completion tokens in words)."""
    return " ".join(f"{tag}{i}" for i in range(words))


def cut_read(tag: str, path: str = "a", words: int = 8) -> FakeReply:
    """A read whose thinking ran to an 8-token budget: reasoning only, so its completion tokens are the
    budget."""
    return FakeReply(
        reasoning=thought(words, tag),
        tool_calls=[ToolCall(f"c-{tag}", "read_file", json.dumps({"path": path}))],
    )


def carried(engine: FakeEngine, i: int) -> list[str]:
    """The carried-reasoning messages in the i-th (1-based) request."""
    said = [str(m.content) for m in engine.requests[i - 1].messages if m.role == "user"]
    return [s for s in said if CUT in s]


def thinking_team(tmp_path: Path, engine: FakeEngine, **role_fields: Any) -> tuple[NativeTeam, TeamContext]:
    fields = {"thinking": True, "thinking_budget": 8, "max_tokens": 64, **role_fields}
    return make_team(tmp_path, engine, **fields)


async def test_a_read_after_thinking_cut_at_the_budget_gets_its_reasoning_back_next_turn(
    tmp_path: Path,
) -> None:
    """AIR-30 (AIR-28's coder, 2026-10-18): a turn cut at the thinking budget read one more file, and the next
    turn began the same argument from zero. Its reasoning comes back, after the tool result, to go on from."""
    engine = FakeEngine({"coder": [cut_read("a"), FakeReply(text="Done.")]})
    team, ctx = thinking_team(tmp_path, engine)
    await team.run(ctx, Recorder())
    assert carried(engine, 1) == []
    assert carried(engine, 2) == [f"{CUT}\n{thought(8, 'a')}\n\n{GO_ON}"]
    last = engine.requests[1].messages
    assert [m.role for m in last[-2:]] == ["tool", "user"]  # right after the observation


async def test_each_cut_appends_its_own_carried_reasoning_and_the_earlier_one_stays(tmp_path: Path) -> None:
    """Until 2026-10-24 the next cut replaced the message carried before it. Removing a message from the
    middle of the history changes every prompt token after it, so the engine's stored prefix (keyed by the
    whole prompt up to the generation prompt) no longer matched and the next step re-prefilled the whole
    conversation: 20.5-35.6 s of prefill against ~2 s on the other steps, 47 s on a 39k prompt in a live run
    on 2026-10-23, 14 carries since 2026-10-19 (docs/bench/2026-10-23-performance-review.md, finding 3).
    Each carry now goes after its own tool result and the earlier ones stay, so every prompt is a prefix of
    the next; the ~400 stale tokens a kept carry costs are cheaper than the re-prefill."""
    engine = FakeEngine({"coder": [cut_read("a"), cut_read("b", "b"), FakeReply(text="Done.")]})
    team, ctx = thinking_team(tmp_path, engine)
    await team.run(ctx, Recorder())
    a, b = (f"{CUT}\n{thought(8, t)}\n\n{GO_ON}" for t in "ab")
    assert carried(engine, 2) == [a]
    assert carried(engine, 3) == [a, b]
    history = engine.requests[2].messages
    before_each = [history[i - 1] for i, m in enumerate(history) if m.role == "user" and CUT in m.content]
    assert [(m.role, m.tool_call_id) for m in before_each] == [("tool", "c-a"), ("tool", "c-b")]
    assert history[: len(engine.requests[1].messages)] == engine.requests[1].messages  # the prefix holds


async def test_a_turn_under_the_budget_leaves_the_last_carried_reasoning_where_it_is(tmp_path: Path) -> None:
    under = FakeReply(reasoning=thought(3, "u"), tool_calls=[ToolCall("u", "read_file", '{"path": "u"}')])
    engine = FakeEngine({"coder": [cut_read("a"), under, FakeReply(text="Done.")]})
    team, ctx = thinking_team(tmp_path, engine)
    await team.run(ctx, Recorder())
    assert carried(engine, 3) == [f"{CUT}\n{thought(8, 'a')}\n\n{GO_ON}"]


async def test_only_the_tail_of_the_reasoning_is_carried(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [cut_read("a", words=20), FakeReply(text="Done.")]})
    team, ctx = thinking_team(tmp_path, engine, carried_reasoning_chars=10)
    await team.run(ctx, Recorder())
    assert carried(engine, 2) == [f"{CUT}\n{thought(20, 'a')[-10:]}\n\n{GO_ON}"]


@pytest.mark.parametrize(
    ("reply", "fields"),
    [
        (FakeReply(reasoning=thought(7), tool_calls=[ToolCall("c", "read_file", '{"path": "a"}')]), {}),
        (
            FakeReply(
                reasoning=thought(8),
                tool_calls=[ToolCall("c", "write_file", json.dumps({"path": "a.py", "content": "x=1\n"}))],
            ),
            {},
        ),
        (FakeReply(reasoning=thought(8), tool_calls=[ToolCall("c", "run_tests", "{}")]), {}),
        (cut_read("a"), {"carried_reasoning_chars": 0}),
        (cut_read("a"), {"thinking_budget": None}),
        (FakeReply(text=thought(8), tool_calls=[ToolCall("c", "read_file", '{"path": "a"}')]), {}),
    ],
    ids=["under-budget", "a-write", "run-tests", "switched-off", "uncapped", "no-reasoning"],
)
async def test_nothing_is_carried_from_a_turn_that_was_not_a_read_cut_at_the_budget(
    tmp_path: Path, reply: FakeReply, fields: dict[str, Any]
) -> None:
    engine = FakeEngine({"coder": [reply, FakeReply(text="Done.")]})
    team, ctx = thinking_team(tmp_path, engine, **fields)
    await team.run(ctx, Recorder())
    assert carried(engine, 2) == []


async def test_a_role_without_thinking_never_gets_reasoning_carried(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [cut_read("a"), FakeReply(text="Done.")]})
    team, ctx = thinking_team(tmp_path, engine, thinking=False)
    await team.run(ctx, Recorder())
    assert carried(engine, 2) == []


async def test_carrying_reasoning_is_a_reasoning_carried_event(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [cut_read("a"), cut_read("b", "b"), FakeReply(text="Done.")]})
    team, ctx = thinking_team(tmp_path, engine)
    events: list[tuple[str, dict[str, Any]]] = []
    ctx.emit = lambda kind, **f: events.append((kind, f))
    await team.run(ctx, Recorder())
    n = len(thought(8, "a"))
    assert [f for k, f in events if k == "reasoning_carried"] == [
        {"team_id": ctx.team_id, "role": ctx.role_name, "step": 1, "chars": n},
        {"team_id": ctx.team_id, "role": ctx.role_name, "step": 2, "chars": n},
    ]


async def test_a_resumed_conversation_keeps_the_reasoning_its_last_run_carried(tmp_path: Path) -> None:
    """The last run's carried message is history like any other (it sits after its tool result and holds):
    the resumed run adds its own after it rather than editing the middle of the prompt."""
    first = FakeEngine({"coder": [cut_read("a")]})
    team, ctx = thinking_team(tmp_path, first, max_steps=1)
    ctx.stop_when = lambda step: True
    state = (await team.run(ctx, Recorder())).state
    engine = FakeEngine({"coder": [cut_read("b", "b"), FakeReply(text="Done.")]})
    team, ctx = thinking_team(tmp_path, engine)
    ctx.initial_state = state
    await team.run(ctx, Recorder())
    a, b = (f"{CUT}\n{thought(8, t)}\n\n{GO_ON}" for t in "ab")
    assert carried(engine, 1) == [a]
    assert carried(engine, 2) == [a, b]


# ---- each steering text the loop sends comes from the prompt hub and is a steer event (2026-10-22) ----


def steers(events: Events) -> list[dict[str, Any]]:
    """The `steer` events, in order, without their kind."""
    return [{k: v for k, v in e.items() if k != "kind"} for e in events.records if e["kind"] == "steer"]


async def test_a_nudge_emits_a_steer_event_with_its_id(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [*reads(5), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10)  # the write-now line: step 4 (10 * 2 // 5)
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    assert steers(events) == [{"id": "write-now", "team_id": "team-1", "role": "coder", "step": 4}]
    assert user_said(engine).count(text("write-now", n=4, total=10)) == 1


async def test_an_advised_repeat_still_reports_the_hub_id(tmp_path: Path) -> None:
    """on_stuck rewrites the repeat nudge; its event names the hub text it started from. The hold and its
    refusals that follow are steering too, each an event at the step it was sent after."""
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [same] * 12})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    ctx.on_stuck = RecordingAdvisor("Stop reading a; write the test.")
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    fixed = text("repeated", n=3, tool="read_file")
    assert f"{fixed}\n\n{SUPERVISOR}\nStop reading a; write the test." in user_said(engine)
    assert [(s["id"], s["step"]) for s in steers(events)] == [
        ("repeated", 3),
        ("repeat-write-only", 6),
        ("read-refused", 7),
        ("read-refused", 8),
        ("write-now", 8),
        ("read-refused", 9),
    ]


async def test_carried_reasoning_is_a_steer_event_as_well(tmp_path: Path) -> None:
    """AIR-30's reasoning_carried event stays, with its chars; the carried text is steering, so it is a
    steer event too."""
    engine = FakeEngine({"coder": [cut_read("a"), FakeReply(text="Done.")]})
    team, ctx = thinking_team(tmp_path, engine)
    ctx.emit = events = Events()
    await team.run(ctx, Recorder())
    assert steers(events) == [{"id": "reasoning-carried", "team_id": "team-1", "role": "coder", "step": 1}]
    assert [e["chars"] for e in events.records if e["kind"] == "reasoning_carried"] == [len(thought(8, "a"))]
    assert carried(engine, 2) == [text("reasoning-carried", tail=thought(8, "a"))]


async def test_context_pressure_is_said_once_and_is_a_steer_event(tmp_path: Path) -> None:
    """At 75% of the role's max_context the role is told once to stop reading (2026-10-19, 9d6cdff)."""
    engine = FakeEngine({"coder": [*reads(2), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_context=200)
    ctx.goal = " ".join(["word"] * 120)  # the first prompt: ~160 words, over 150 and under 200
    ctx.emit = events = Events()
    rec = Recorder()
    result = await team.run(ctx, rec)
    assert result.ok
    said = text("context-pressure", max_prompt=rec.steps[0].prompt_tokens, max_context=200)
    assert user_said(engine).count(said) == 1
    assert steers(events) == [{"id": "context-pressure", "team_id": "team-1", "role": "coder", "step": 1}]
    assert [e["kind"] for e in events.records].count("context") == 1


def test_a_resumed_run_drops_hub_end_of_budget_messages() -> None:
    """The end-of-budget messages are recognised from the hub's text (prompt hub review focus 3), so a
    history saved with them still loses them on resume; other nudges in it stay."""
    write_now = text("write-now", n=4, total=10)
    history = [
        Message(role="system", content="You are the coder."),
        Message(role="user", content="Goal:\nmake it pass"),
        Message(role="user", content=entry("one-step-left").body),
        Message(role="user", content=write_now),
        Message(role="user", content=text("last-step", n=40)),
        Message(role="user", content=text("last-turn-retry")),
        Message(role="user", content=text("no-tools")),
    ]
    kept, dropped = _without_stale_budget(load_messages(dump_messages(history)))
    assert dropped
    assert [m.content for m in kept] == ["You are the coder.", "Goal:\nmake it pass", write_now]
    assert _without_stale_budget(kept) == (kept, False)


async def test_the_opening_messages_are_told_to_on_prompt_without_the_picture_bytes(tmp_path: Path) -> None:
    pic = tmp_path / "shot.png"
    pic.write_bytes(b"\x89PNG" + b"0" * 5000)
    engine = FakeEngine({"coder": [FakeReply(text="done")]})
    team, ctx = make_team(tmp_path, engine)
    told: list[list[dict[str, Any]]] = []
    ctx.images = [pic]
    ctx.on_prompt = told.append
    await team.run(ctx, Recorder())
    (opening,) = told
    assert [m["role"] for m in opening] == ["system", "user"]
    assert "make it pass" in opening[1]["content"]
    assert "data:image" not in json.dumps(opening) and "<image," in json.dumps(opening)


async def test_a_think_when_red_role_thinks_only_while_its_last_test_run_failed(tmp_path: Path) -> None:
    """Roman, 2026-10-28: the non-thinking coder, red, rewrote the same wrong file three times; its first
    attempt stays fast and unthinking, and it thinks from a failed run_tests until a run passes."""
    run = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])
    write = FakeReply(
        tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))]
    )
    engine = FakeEngine({"coder": [write, run, write, run, FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10, think_when_red=True, thinking_budget=32)
    object.__setattr__(ctx.tools.ctx, "test_cmd", "exit 1")  # every run fails: from step 3 on it thinks
    await team.run(ctx, Recorder())
    assert [r.thinking for r in engine.requests] == [False, False, True, True, True]
    told = [str(m.content) for m in engine.requests[2].messages if m.role == "tool"]
    assert "do not read a file you wrote in this conversation" in told[-1]  # each red run says what is next
    assert sum("make the one edit_file" in str(m.content) for m in engine.requests[-1].messages) == 2
    engine = FakeEngine({"coder": [write, run, write, FakeReply(text="Done.")]})
    (tmp_path / "green").mkdir()
    team, ctx = make_team(tmp_path / "green", engine, max_steps=10, think_when_red=True, thinking_budget=32)
    await team.run(ctx, Recorder())  # `echo ok` passes: never red, never thinking
    assert [r.thinking for r in engine.requests] == [False, False, False, False]


async def test_a_think_when_red_role_does_not_read_back_its_own_file_while_red(tmp_path: Path) -> None:
    """Roman, 2026-10-28: the red coder read back what it had written; refusing every read instead left it
    unable to see which input a looped test failed on (35 minutes red). While red, only a re-read of its own
    file is refused; another file, or the same one once green, is read."""
    run = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])
    write = FakeReply(
        tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))]
    )

    async def last_tool_output(test_cmd: str, path: str, workdir: Path) -> tuple[str, list[str]]:
        read = FakeReply(tool_calls=[ToolCall("r", "read_file", json.dumps({"path": path}))])
        engine = FakeEngine({"coder": [write, run, read, FakeReply(text="Done.")]})
        team, ctx = make_team(workdir, engine, max_steps=10, think_when_red=True, thinking_budget=32)
        object.__setattr__(ctx.tools.ctx, "test_cmd", test_cmd)
        events: list[str] = []
        object.__setattr__(ctx, "emit", lambda kind, **f: events.append(kind))
        await team.run(ctx, Recorder())
        told = [str(m.content) for m in engine.requests[3].messages if m.role == "tool"][-1]
        return told, [k for k in events if k == "read_refused"]

    told, refused = await last_tool_output("exit 1", "./a.py", tmp_path)
    assert told.startswith("This is a file you wrote in this conversation") and refused == ["read_refused"]
    for n, (cmd, path) in enumerate((("exit 1", "README.md"), ("echo ok", "a.py"))):
        (tmp_path / str(n)).mkdir()
        (tmp_path / str(n) / "README.md").write_text("# hi\n")
        told, refused = await last_tool_output(cmd, path, tmp_path / str(n))
        assert refused == [] and "wrote in this conversation" not in told  # another file, or green: read


async def test_a_red_streak_asks_the_advisor_and_tells_it_what_came_of_it(tmp_path: Path) -> None:
    """Roman, 2026-10-28: 35 minutes red on one test, each edit different, so the repeat guard never fired
    and the supervisor never looked. Every RED_STREAK failed runs in a row it is asked; its advice follows
    the red-fix text."""
    from airlie.teams.native_team import RED_STREAK
    from tests.unit.advisor_doubles import RecordingAdvisor

    run = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])

    def edit(n: int) -> FakeReply:
        content = json.dumps({"path": "a.py", "content": f"x = {n}\n"})
        return FakeReply(tool_calls=[ToolCall(f"w{n}", "write_file", content)])

    script = [x for n in range(RED_STREAK) for x in (edit(n), run)] + [FakeReply(text="Done.")]
    engine = FakeEngine({"coder": script})
    team, ctx = make_team(tmp_path, engine, max_steps=20, think_when_red=True, thinking_budget=32)
    object.__setattr__(ctx.tools.ctx, "test_cmd", "exit 1")
    advisor = RecordingAdvisor("from_roman lets IIII through: add a repeat check")
    object.__setattr__(ctx, "on_stuck", advisor)
    await team.run(ctx, Recorder())
    assert len(advisor.red_calls) == 1 and advisor.red_calls[0][1] == "coder"
    told = [str(m.content) for m in engine.requests[-1].messages if m.role == "tool"][-1]
    assert told.endswith("from_roman lets IIII through: add a repeat check")  # after red-fix
    assert [helped for _, helped in advisor.red_settles] == [False]  # the run ended red


async def test_a_red_streak_diagnosed_as_a_wrong_test_stops_the_role(tmp_path: Path) -> None:
    """Roman, 2026-10-28: the supervisor said a locked test was wrong ('MMMDCCCLXXXIX' for 3999) and the coder
    edited on for a 31-minute fix round. No code fixes a wrong test: the role stops, test_wrong."""
    from airlie.supervisor.advice import WRONG_TEST
    from airlie.teams.native_team import RED_STREAK
    from tests.unit.advisor_doubles import RecordingAdvisor

    run = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])

    def edit(n: int) -> FakeReply:
        content = json.dumps({"path": "a.py", "content": f"x = {n}\n"})
        return FakeReply(tool_calls=[ToolCall(f"w{n}", "write_file", content)])

    script = [x for n in range(RED_STREAK + 1) for x in (edit(n), run)] + [FakeReply(text="Done.")]
    engine = FakeEngine({"coder": script})
    team, ctx = make_team(tmp_path, engine, max_steps=20, think_when_red=True, thinking_budget=32)
    object.__setattr__(ctx.tools.ctx, "test_cmd", "exit 1")
    object.__setattr__(ctx, "on_stuck", RecordingAdvisor("line 17: MMMDCCCLXXXIX is 3889", WRONG_TEST))
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "test_wrong" and not result.ok
    assert result.steps == 2 * RED_STREAK  # it stopped at the streak: no further edit ran
    assert "MMMDCCCLXXXIX is 3889" in result.final_text


async def test_a_repeat_diagnosed_as_a_wrong_test_stops_the_role(tmp_path: Path) -> None:
    """Roman, 2026-10-30: the repeat guard's advice said a locked test was wrong (13 characters, the longest
    numeral is 15) and the coder went on in shell for 40 minutes. As after a red streak, it stops."""
    from airlie.supervisor.advice import WRONG_TEST
    from tests.unit.advisor_doubles import RecordingAdvisor

    probe = FakeReply(tool_calls=[ToolCall("s", "shell", json.dumps({"cmd": "python3 -c 1"}))])
    engine = FakeEngine({"coder": [probe] * 8 + [FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    object.__setattr__(
        ctx, "on_stuck", RecordingAdvisor("the bound is 15, not 13: the test is wrong", WRONG_TEST)
    )
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "test_wrong" and not result.ok
    assert "the bound is 15, not 13" in result.final_text
    assert result.steps == ctx.repeat_limit  # it stopped at the guard


async def test_different_programs_that_print_the_same_are_not_a_repeat(tmp_path: Path) -> None:
    """2026-10-31 (roman): product's three different checks of one table printed the same, and the repeat
    guard stopped its refine (repeated_action). compute counts calls apart by their program."""

    def check(n: int) -> FakeReply:
        return FakeReply(
            tool_calls=[ToolCall(f"c{n}", "compute", json.dumps({"code": f"print(15)  # check {n}"}))]
        )

    engine = FakeEngine({"coder": [check(1), check(2), check(3), check(4), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10, tool_names=("compute",))
    result = await team.run(ctx, Recorder())
    assert result.stop_reason != "repeated_action" and result.final_text == "Done."


async def test_the_same_read_again_is_answered_from_the_conversation_until_the_file_is_written(
    tmp_path: Path,
) -> None:
    """Roman A/B run 2 (2026-10-29): the architect read one test file six times in a row, each step thinking
    to its budget, until the repeat guard stopped its drafting. A repeat of the same read, with nothing
    written to the file since, does not run; a write to it, or another range, reads afresh."""

    def read(**args: object) -> FakeReply:
        return FakeReply(tool_calls=[ToolCall("r", "read_file", json.dumps({"path": "a.py", **args}))])

    write = FakeReply(
        tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "a.py", "content": "x = 2\n"}))]
    )
    engine = FakeEngine({"coder": [read(), read(), read(offset=1), write, read(), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=10)
    (ctx.tools.ctx.worktree / "a.py").write_text("x = 1\n")
    events: list[dict[str, Any]] = []
    object.__setattr__(ctx, "emit", lambda kind, **f: events.append({"kind": kind, **f}))
    await team.run(ctx, Recorder())
    outputs = [str(m.content) for m in engine.requests[-1].messages if m.role == "tool"]
    assert "x = 1" in outputs[0] and outputs[1].startswith("You read this file at step 1")
    assert "You read this file" not in outputs[2]  # another range is another read
    assert "x = 2" in outputs[4]  # written since: read afresh
    assert [e["step"] for e in events if e["kind"] == "read_repeated"] == [2]


async def test_a_role_red_for_red_limit_runs_in_a_row_stops_for_the_architect(tmp_path: Path) -> None:
    """R1 (2026-10-29): a failed build spent ~31 minutes red before its layer was re-planned, and the re-plan
    is what fixed it. After RED_LIMIT failed runs in a row the role stops (red_limit)."""
    from airlie.teams.native_team import RED_LIMIT

    run = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])

    def edit(n: int) -> FakeReply:
        content = json.dumps({"path": "a.py", "content": f"x = {n}\n"})
        return FakeReply(tool_calls=[ToolCall(f"w{n}", "write_file", content)])

    script = [x for n in range(RED_LIMIT + 2) for x in (edit(n), run)] + [FakeReply(text="Done.")]
    engine = FakeEngine({"coder": script})
    team, ctx = make_team(tmp_path, engine, max_steps=40, think_when_red=True, thinking_budget=32)
    object.__setattr__(ctx.tools.ctx, "test_cmd", "exit 1")
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "red_limit" and not result.ok
    assert result.steps == 2 * RED_LIMIT and f"failed {RED_LIMIT} runs in a row" in result.final_text
