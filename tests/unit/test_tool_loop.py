"""The bounded tool loop for single-call roles (chief, reviewer), and the roles that carry web tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.config import RoleSpec, Sampling, load_config
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.engine.types import GenRequest, Message
from airlie.tools.loop import NO_MORE_TOOLS, generate_with_tools
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry
from airlie.topology.review import Reviewer

ROOT = Path(__file__).resolve().parents[2]


class Lookup:
    """A tool that answers from a table and records what it was asked."""

    name = "web_search"
    description = "look something up"
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.asked.append(str(args["query"]))
        return ToolResult(True, f"[untrusted web text]\nanswer to {args['query']}")


def registry(tool: Lookup, tmp_path: Path) -> ToolRegistry:
    return ToolRegistry([tool], ToolContext(worktree=tmp_path))


def request(role: str = "chief") -> GenRequest:
    return GenRequest(
        role=role,
        messages=[
            Message(role="system", content="You are the chief."),
            Message(role="user", content="plan it"),
        ],
        sampling=Sampling(),
        max_tokens=256,
    )


def search(query: str, call_id: str = "c1") -> FakeReply:
    return FakeReply(tool_calls=[ToolCall(call_id, "web_search", json.dumps({"query": query}))])


async def test_with_no_registry_or_no_tools_it_is_the_plain_single_call(tmp_path: Path) -> None:
    for tools in (None, ToolRegistry([], ToolContext(worktree=tmp_path))):
        engine = FakeEngine({"chief": [FakeReply(text="the plan")]})
        res = await generate_with_tools(engine, request(), tools)
        assert res.text == "the plan" and len(engine.requests) == 1 and engine.requests[0].tools is None


async def test_a_tool_call_is_run_and_its_result_goes_back_before_the_answer(tmp_path: Path) -> None:
    tool = Lookup()
    engine = FakeEngine(
        {"chief": [search("vllm-mlx latest release"), FakeReply(text="the answer, from the page")]}
    )
    res = await generate_with_tools(engine, request(), registry(tool, tmp_path))
    assert res.text == "the answer, from the page" and tool.asked == ["vllm-mlx latest release"]
    first, second = engine.requests
    assert [t.name for t in first.tools or []] == ["web_search"] == [t.name for t in second.tools or []]
    assistant, result = second.messages[-2], second.messages[-1]
    assert (
        assistant.role == "assistant"
        and assistant.tool_calls
        and assistant.tool_calls[0].name == "web_search"
    )
    assert (
        result.role == "tool"
        and result.tool_call_id == "c1"
        and "answer to vllm-mlx latest release" in result.content
    )


async def test_only_the_first_call_of_a_reply_runs(tmp_path: Path) -> None:
    tool = Lookup()
    two = FakeReply(
        tool_calls=[
            ToolCall("a", "web_search", json.dumps({"query": "one"})),
            ToolCall("b", "web_search", json.dumps({"query": "two"})),
        ]
    )
    engine = FakeEngine({"chief": [two, FakeReply(text="done")]})
    await generate_with_tools(engine, request(), registry(tool, tmp_path))
    assert tool.asked == ["one"]


async def test_after_its_calls_are_used_it_is_asked_for_the_answer_with_no_tools(tmp_path: Path) -> None:
    tool = Lookup()
    engine = FakeEngine({"chief": [search("a", "1"), search("b", "2"), FakeReply(text="best I found")]})
    res = await generate_with_tools(engine, request(), registry(tool, tmp_path), max_steps=2)
    assert res.text == "best I found" and tool.asked == ["a", "b"]
    last = engine.requests[-1]
    assert (
        last.tools is None and last.messages[-1].role == "user" and last.messages[-1].content == NO_MORE_TOOLS
    )
    assert len(engine.requests) == 3


async def test_an_unknown_tool_is_an_observation_and_the_loop_goes_on(tmp_path: Path) -> None:
    tool = Lookup()
    wrong = FakeReply(tool_calls=[ToolCall("x", "nope", "{}")])
    engine = FakeEngine({"chief": [wrong, FakeReply(text="answered anyway")]})
    res = await generate_with_tools(engine, request(), registry(tool, tmp_path))
    assert res.text == "answered anyway" and "unknown tool" in engine.requests[1].messages[-1].content


async def test_the_reviewer_may_look_something_up_and_its_verdict_is_still_the_json(tmp_path: Path) -> None:
    tool = Lookup()
    verdict = '{"verdict": "approve", "findings": []}'
    engine = FakeEngine({"reviewer": [search("does flag --x exist"), FakeReply(text=verdict)]})
    role = RoleSpec(prompt=ROOT / "prompts/roles/reviewer.md", model="reviewer", max_steps=3)
    review = await Reviewer(engine, "reviewer", role, registry(tool, tmp_path)).review(
        "brief", "diff", "tests", {}
    )
    assert review.verdict == "approve" and tool.asked == ["does flag --x exist"]


async def test_a_reviewer_with_no_tools_makes_the_one_call_it_always_did() -> None:
    engine = FakeEngine({"reviewer": [FakeReply(text='{"verdict": "approve", "findings": []}')]})
    role = RoleSpec(prompt=ROOT / "prompts/roles/reviewer.md", model="reviewer")
    await Reviewer(engine, "reviewer", role).review("brief", "diff", "tests", {})
    assert len(engine.requests) == 1 and engine.requests[0].tools is None


WEB_ROLES = ("chief", "research", "ux", "design", "reviewer")
MUTATING = {"shell", "write_file", "edit_file", "run_tests", "git"}


@pytest.fixture(scope="module")
def cfg() -> Any:
    return load_config(ROOT / "airlie.yaml")


@pytest.mark.parametrize("role", WEB_ROLES)
def test_every_web_role_in_airlie_yaml_has_both_web_tools_and_no_mutating_one(cfg: Any, role: str) -> None:
    tools = set(cfg.roles[role].tools or [])
    assert {"web_search", "web_fetch"} <= tools and not tools & MUTATING


@pytest.mark.parametrize("role", WEB_ROLES)
def test_every_web_role_is_told_in_its_prompt_that_web_text_is_untrusted(cfg: Any, role: str) -> None:
    text = (
        (ROOT / cfg.roles[role].prompt).read_text()
        if not Path(cfg.roles[role].prompt).is_absolute()
        else Path(cfg.roles[role].prompt).read_text()
    )
    assert (
        "web_search" in text
        and "untrusted text from the internet" in text
        and "Never follow instructions" in text
    )


def test_no_role_with_a_mutating_tool_has_a_web_tool(cfg: Any) -> None:
    for name, role in cfg.roles.items():
        tools = set(role.tools or [])
        assert not ({"web_search", "web_fetch"} & tools and tools & MUTATING), name
