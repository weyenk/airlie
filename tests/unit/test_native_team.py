"""Native ReAct team loop over the Engine seam: tool calls become steps, observers see (u_t, o_t)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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


async def test_a_role_that_still_has_not_written_four_steps_after_the_nudge_keeps_only_the_write_tools(
    tmp_path: Path,
) -> None:
    engine = FakeEngine({"coder": [*reads(9), FakeReply(text="Done.")]})
    team, ctx = make_team(
        tmp_path, engine, max_steps=12
    )  # nudge at step 4 (12 * 2 // 5 = 4), reading off at 8
    await team.run(ctx, Recorder())
    offered = [{t.name for t in (r.tools or [])} for r in engine.requests]
    assert offered[7] == {"write_file", "read_file", "run_tests"}  # the eighth request: still all of them
    assert offered[8] == {"write_file", "run_tests"} == offered[9]  # from the ninth, no reading
    told = [
        i
        for i, r in enumerate(engine.requests, 1)
        if any("reading is switched off" in str(m.content) for m in r.messages)
    ]
    assert told[0] == 9 and told == list(range(9, len(engine.requests) + 1))


async def test_a_role_that_writes_after_the_nudge_keeps_its_read_tools(tmp_path: Path) -> None:
    write = FakeReply(
        tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "a.py", "content": "x = 1\n"}))]
    )
    engine = FakeEngine({"coder": [*reads(4), write, *reads(5), FakeReply(text="Done.")]})
    team, ctx = make_team(tmp_path, engine, max_steps=14)
    await team.run(ctx, Recorder())
    assert all("read_file" in {t.name for t in (r.tools or [])} for r in engine.requests[:-1])


async def test_a_role_with_nudge_stuck_is_told_once_to_do_something_else_and_stopped_on_the_next_repeat(
    tmp_path: Path,
) -> None:
    same = FakeReply(tool_calls=[ToolCall("c", "read_file", json.dumps({"path": "a"}))])
    engine = FakeEngine({"coder": [same] * 9})
    team, ctx = make_team(tmp_path, engine, max_steps=20, nudge_stuck=True)
    result = await team.run(ctx, Recorder())
    assert result.stop_reason == "repeated_action" and result.steps == 6  # 3 repeats, told, 3 more repeats
    told = [
        i
        for i, r in enumerate(engine.requests, 1)
        if any("same call 3 times in a row" in str(m.content) for m in r.messages)
    ]
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
