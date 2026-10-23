"""AutoGen team adapter: events map to StepRecords; tools wrap the registry; client targets the engine."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest
from autogen_agentchat.messages import ToolCallExecutionEvent, ToolCallRequestEvent
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, FunctionExecutionResult, RequestUsage
from autogen_ext.models.openai import OpenAIChatCompletionClient

from airlie.config import RoleSpec
from airlie.engine import ToolCall
from airlie.scheduler import SlotScheduler
from airlie.teams import NullObserver, TeamContext
from airlie.teams.autogen_team import AutoGenTeam, build_function_tools, build_model_client, pair_steps
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


async def test_autogen_state_round_trip_and_nudge(tmp_path: Path) -> None:
    """save_state after a fork point reloads into a fresh agent; the nudge is the next task."""
    from autogen_agentchat.agents import AssistantAgent
    from autogen_ext.models.replay import ReplayChatCompletionClient

    from airlie.teams.autogen_team import agent_state, load_agent_state

    a = AssistantAgent("trunk", model_client=ReplayChatCompletionClient(["first"]), system_message="sys")
    await a.run(task="Goal: x")
    state = await agent_state(a)
    assert state["type"] == "AssistantAgentState" and len(state["llm_context"]["messages"]) == 2
    b = AssistantAgent("child", model_client=ReplayChatCompletionClient(["second"]), system_message="sys")
    await load_agent_state(b, state)
    msgs = await b.model_context.get_messages()
    assert [m.content for m in msgs] == ["Goal: x", "first"]


async def test_registry_tool_marks_failures_as_errors_and_names_missing_args(tmp_path: Path) -> None:
    """Live finding 2026-10-13: AutoGen only sets is_error when the tool raises, so a failing run_tests looked
    like a pass to the fork trigger. Failures raise ToolError; a missing required argument names it."""
    import pytest

    from airlie.teams.autogen_team import ToolError

    wt = tmp_path / "wt"
    wt.mkdir()
    reg = ToolRegistry.builtin(
        ["read_file", "write_file", "run_tests"], ToolContext(worktree=wt, test_cmd="false")
    )
    read, write, run_tests = build_function_tools(reg)
    with pytest.raises(
        ToolError, match=r"write_file requires: (path, content|content, path)\. A correct call looks like: "
    ):
        await write.run_json({}, cancellation_token=None)  # type: ignore[arg-type]
    with pytest.raises(ToolError, match="no such file|not found|error"):
        await read.run_json({"path": "missing.txt"}, cancellation_token=None)  # type: ignore[arg-type]
    with pytest.raises(ToolError):
        await run_tests.run_json({}, cancellation_token=None)  # type: ignore[arg-type]


def test_model_client_passes_sampling_the_openai_client_lacks_via_extra_body(tmp_path: Path) -> None:
    from airlie.config import Sampling

    (tmp_path / "p.md").write_text("x")
    role = RoleSpec(
        prompt=tmp_path / "p.md",
        model="base",
        thinking=False,
        max_tokens=64,
        sampling=Sampling(top_k=20, min_p=0.05, repetition_penalty=1.05),
    )
    client = build_model_client("http://127.0.0.1:8080/v1", "base", role, api_key=None)
    extra = client._create_args["extra_body"]  # noqa: SLF001 - what the OpenAI client sends per request
    assert extra == {"top_k": 20, "min_p": 0.05, "repetition_penalty": 1.05}


async def test_registry_tool_reports_cut_off_arguments(tmp_path: Path) -> None:
    """A runaway generation leaves the arguments JSON unterminated; the model gets a clear, actionable
    error."""
    import pytest

    from airlie.teams.autogen_team import ToolError

    wt = tmp_path / "wt"
    wt.mkdir()
    reg = ToolRegistry.builtin(["write_file"], ToolContext(worktree=wt))
    (write,) = build_function_tools(reg)
    with pytest.raises(ToolError, match="cut off.*shorter"):
        await write.run_json({"__truncated__": "0000000000"}, cancellation_token=None)  # type: ignore[arg-type]


# ---- a reply cut off at max_tokens is not a finished answer (HDS-3 follow-up, 2026-10-15) ----

USAGE = RequestUsage(prompt_tokens=10, completion_tokens=64)


def reply(finish: Any, content: Any = "", thought: str | None = None) -> CreateResult:
    return CreateResult(finish_reason=finish, content=content, usage=USAGE, cached=False, thought=thought)


def scripted_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *replies: CreateResult, **role_fields: Any
) -> tuple[AutoGenTeam, TeamContext]:
    """The real AutoGenTeam, agent and repairing client, with the OpenAI client under them replaced by a
    script of results (no network)."""
    script = deque(replies)

    async def create(self: Any, messages: Any, **kwargs: Any) -> CreateResult:
        return script.popleft()

    monkeypatch.setattr(OpenAIChatCompletionClient, "create", create)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "a.py").write_text("x = 1\n")
    (tmp_path / "p.md").write_text("You are the architect.")
    role = RoleSpec(
        prompt=tmp_path / "p.md", model="base", **{"thinking": False, "max_tokens": 64, **role_fields}
    )
    ctx = TeamContext(
        team_id="team-1",
        goal="plan the layers",
        role=role,
        tools=ToolRegistry.builtin(["read_file"], ToolContext(worktree=wt)),
        worktree=wt,
        role_name="architect",
        scheduler=SlotScheduler(1),
    )
    return AutoGenTeam("http://127.0.0.1:1/v1", "base"), ctx


async def test_autogen_a_reply_cut_off_with_no_visible_text_is_not_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cut = reply("length", thought="Let me read the files again.")
    team, ctx = scripted_team(tmp_path, monkeypatch, cut, thinking=True, thinking_budget=100, max_tokens=64)
    result = await team.run(ctx, NullObserver())
    assert result.ok is False and result.stop_reason == "cut_off" and result.steps == 0
    assert result.final_text == (
        "the reply was cut off at max_tokens (64) before it ended, still thinking; "
        "raise roles.architect.max_tokens above its thinking_budget (100)"
    )
    assert result.completion_tokens == 64


async def test_autogen_a_cut_off_reply_that_has_text_keeps_it_and_is_marked_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team, ctx = scripted_team(tmp_path, monkeypatch, reply("length", "The plan has three lay"))
    result = await team.run(ctx, NullObserver())
    assert (result.ok, result.stop_reason, result.final_text) == (False, "cut_off", "The plan has three lay")


async def test_autogen_a_cut_off_reply_without_thinking_says_only_what_to_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team, ctx = scripted_team(tmp_path, monkeypatch, reply("length", "  "))
    result = await team.run(ctx, NullObserver())
    assert result.stop_reason == "cut_off"
    assert (
        result.final_text
        == "the reply was cut off at max_tokens (64) before it ended; raise roles.architect.max_tokens"
    )


async def test_autogen_a_reply_that_stopped_by_itself_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team, ctx = scripted_team(tmp_path, monkeypatch, reply("stop", "All done.", thought="Thinking."))
    result = await team.run(ctx, NullObserver())
    assert (result.ok, result.stop_reason, result.final_text) == (True, "done", "All done.")


async def test_autogen_a_tool_call_is_unaffected_by_a_length_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = reply("length", [FunctionCall(id="c1", name="read_file", arguments='{"path": "a.py"}')])
    team, ctx = scripted_team(tmp_path, monkeypatch, call, reply("stop", "Read it."))
    result = await team.run(ctx, NullObserver())
    assert (result.ok, result.stop_reason, result.final_text, result.steps) == (True, "done", "Read it.", 1)


async def test_autogen_the_client_remembers_how_each_reply_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autogen_core.models import UserMessage

    from airlie.teams.autogen_client import RepairingOpenAIClient

    script = deque([reply("length"), reply("stop", "ok")])

    async def create(self: Any, messages: Any, **kwargs: Any) -> CreateResult:
        return script.popleft()

    async def create_stream(self: Any, messages: Any, **kwargs: Any) -> Any:
        yield "partial "
        yield reply("length", "partial ")

    monkeypatch.setattr(OpenAIChatCompletionClient, "create", create)
    monkeypatch.setattr(OpenAIChatCompletionClient, "create_stream", create_stream)
    (tmp_path / "p.md").write_text("x")
    role = RoleSpec(prompt=tmp_path / "p.md", model="base", thinking=False, max_tokens=64)
    client = build_model_client("http://127.0.0.1:1/v1", "base", role, api_key=None)
    assert isinstance(client, RepairingOpenAIClient) and client.last_finish_reason == ""
    messages = [UserMessage(content="hi", source="user")]
    await client.create(messages)
    assert client.last_finish_reason == "length"
    await client.create(messages)
    assert client.last_finish_reason == "stop"
    items = [item async for item in client.create_stream(messages)]
    assert isinstance(items[-1], CreateResult) and client.last_finish_reason == "length"
