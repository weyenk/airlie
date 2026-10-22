"""Fork-point diversification: hypotheses from the judge role, temperature fallback, nudges (design §3.3)."""

from __future__ import annotations

from pathlib import Path

from airlie.config import RoleSpec
from airlie.engine import FakeEngine, FakeReply
from airlie.topology.diversify import (
    hypotheses,
    nudge_text,
    parse_failing,
    parse_hypotheses,
    role_at_temperature,
)


def test_parse_hypotheses_numbered_lines() -> None:
    text = (
        "Some preamble\n1. total_cost adds instead of multiplying\n2) the CLI parses qty as str\n3. unrelated"
    )
    assert parse_hypotheses(text, 2) == [
        "total_cost adds instead of multiplying",
        "the CLI parses qty as str",
    ]


def test_parse_hypotheses_rejects_short_or_duplicate_output() -> None:
    assert parse_hypotheses("1. only one", 2) is None
    assert parse_hypotheses("1. same\n2. same", 2) is None
    assert parse_hypotheses("no numbers here", 2) is None


async def test_hypotheses_calls_the_role_once_and_falls_back_on_unparsable_output(tmp_path: Path) -> None:
    (tmp_path / "judge.md").write_text("judge")
    role = RoleSpec(prompt=tmp_path / "judge.md", model="small", thinking=False, max_tokens=200)
    engine = FakeEngine({"judge": [FakeReply(text="1. A is wrong\n2. B is wrong"), FakeReply(text="dunno")]})
    got = await hypotheses(engine, "judge", role, goal="g", diff="+x", test_tail="FAILED", width=2)
    assert got == ["A is wrong", "B is wrong"]
    assert engine.requests[-1].role == "judge" and "FAILED" in engine.requests[-1].messages[-1].content
    assert await hypotheses(engine, "judge", role, goal="g", diff="", test_tail="", width=2) is None


async def test_hypotheses_system_message_has_no_front_matter(tmp_path: Path) -> None:
    (tmp_path / "judge.md").write_text("---\nsummary: s\n---\n# coder\nbody\n")
    role = RoleSpec(prompt=tmp_path / "judge.md", model="small", thinking=False, max_tokens=200)
    engine = FakeEngine({"judge": [FakeReply(text="1. A is wrong\n2. B is wrong")]})
    await hypotheses(engine, "judge", role, goal="g", diff="+x", test_tail="FAILED", width=2)
    system = engine.requests[0].messages[0]
    assert system.role == "system" and system.content == "# coder\nbody\n"


def test_nudge_text_names_branch_hypothesis_and_failure() -> None:
    n = nudge_text("r1-2", 2, "the CLI parses qty as str", "FAILED test_x")
    assert n.startswith("You are now branch r1-2 of 2")
    assert "the CLI parses qty as str" in n and "FAILED test_x" in n and "do not repeat" in n.lower()
    m = nudge_text("r1-1", 2, None, "")
    assert "different approach" in m.lower()


def test_role_at_temperature_copies(tmp_path: Path) -> None:
    (tmp_path / "c.md").write_text("c")
    role = RoleSpec(prompt=tmp_path / "c.md", model="base")
    hot = role_at_temperature(role, 0.9)
    assert hot.sampling.temperature == 0.9 and role.sampling.temperature != 0.9 and hot.model == "base"


def test_parse_failing() -> None:
    assert parse_failing("3 failed, 2 passed in 0.1s") == 3
    assert parse_failing("5 passed in 0.1s") == 0
    assert parse_failing("ok\n") is None


def test_nudge_text_names_the_repeated_failing_action() -> None:
    n = nudge_text("r1-1", 2, None, "", repeated=("read_file", "{}", "error: 'path'"))
    assert "read_file" in n and "{}" in n and "error: 'path'" in n and "required arguments" in n.lower()


def test_trim_repeated_tail_native_and_autogen_states() -> None:
    from airlie.topology.diversify import trim_repeated_tail

    native = {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "g"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1", "name": "list_dir", "arguments": "{}"}],
            },
            {"role": "tool", "content": "ok", "tool_call_id": "1", "name": "list_dir"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "2", "name": "read_file", "arguments": "{}"}],
            },
            {"role": "tool", "content": "err", "tool_call_id": "2", "name": "read_file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "3", "name": "read_file", "arguments": "{}"}],
            },
            {"role": "tool", "content": "err", "tool_call_id": "3", "name": "read_file"},
        ]
    }
    out = trim_repeated_tail(native, 2)
    assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant", "tool"]
    assert len(native["messages"]) == 8  # input untouched
    autogen = {
        "type": "AssistantAgentState",
        "llm_context": {
            "messages": [
                {"type": "UserMessage", "content": "g"},
                {"type": "AssistantMessage", "content": [{"name": "read_file", "arguments": "{}"}]},
                {"type": "FunctionExecutionResultMessage", "content": [{"content": "err"}]},
                {"type": "AssistantMessage", "content": [{"name": "read_file", "arguments": "{}"}]},
                {"type": "FunctionExecutionResultMessage", "content": [{"content": "err"}]},
            ]
        },
    }
    out = trim_repeated_tail(autogen, 2)
    assert [m["type"] for m in out["llm_context"]["messages"]] == ["UserMessage"]
    assert trim_repeated_tail({"messages": [{"role": "user", "content": "g"}]}, 3) == {
        "messages": [{"role": "user", "content": "g"}]
    }
