"""Tool-call argument repair: vllm-mlx's qwen3_coder parser can emit trailing braces or truncated JSON."""

from __future__ import annotations

import json

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage

from airlie.engine import ToolCall
from airlie.teams.autogen_client import repair_create_result
from airlie.tools.jsonargs import parse_tool_args, repair_json_args


def test_valid_json_is_untouched() -> None:
    assert repair_json_args('{"a": 1}') == '{"a": 1}'


def test_trailing_brace_is_dropped() -> None:
    bad = '{"path": "x.py", "content": "d = {}\\n"}}'
    assert json.loads(repair_json_args(bad)) == {"path": "x.py", "content": "d = {}\n"}


def test_leading_junk_before_object_is_dropped() -> None:
    assert json.loads(repair_json_args('<tool_call>{"a": 1}')) == {"a": 1}


def test_truncated_json_is_left_for_the_caller_to_report() -> None:
    bad = '{"path": "x.py", "content": "def f():'
    assert repair_json_args(bad) == bad
    with pytest.raises(ValueError, match="arguments"):
        parse_tool_args(bad)


def test_parse_tool_args_rejects_non_objects() -> None:
    with pytest.raises(ValueError):
        parse_tool_args("[1, 2]")


def test_repair_create_result_fixes_args_and_keeps_the_first_call() -> None:
    calls = [
        FunctionCall(id="a", name="write_file", arguments='{"path": "x", "content": "{}"}}'),
        FunctionCall(
            id="b", name="write_file", arguments='{"path": "x", "content": "{}"}}'
        ),  # vllm-mlx double emit
        FunctionCall(id="c", name="shell", arguments='{"cmd": "ls"}'),
    ]
    res = CreateResult(
        finish_reason="function_calls",
        content=calls,
        usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
        cached=False,
    )
    out = repair_create_result(res)
    assert [c.id for c in out.content] == ["a"]  # type: ignore[union-attr]  # first call only (2026-10-13)
    assert json.loads(out.content[0].arguments) == {"path": "x", "content": "{}"}  # type: ignore[union-attr]


def test_repair_create_result_passes_text_through() -> None:
    res = CreateResult(
        finish_reason="stop",
        content="hello",
        usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
        cached=False,
    )
    assert repair_create_result(res).content == "hello"


def test_tool_call_dataclass_round_trip() -> None:
    assert ToolCall("1", "x", "{}").arguments == "{}"


def test_repair_create_result_keeps_only_the_first_call() -> None:
    """Live finding 2026-10-13: the coder sometimes lists a whole plan of calls in one response, the later
    ones without arguments. One action per step (spec 03 §1 triplets); the model re-plans after each
    result."""
    from autogen_core import FunctionCall
    from autogen_core.models import CreateResult, RequestUsage

    from airlie.teams.autogen_client import repair_create_result

    res = CreateResult(
        finish_reason="function_calls",
        content=[
            FunctionCall(id="a", name="run_tests", arguments="{}"),
            FunctionCall(id="b", name="read_file", arguments="{}"),
            FunctionCall(id="c", name="read_file", arguments='{"path": "x"}'),
        ],
        usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
        cached=False,
    )
    out = repair_create_result(res)
    assert isinstance(out.content, list) and [c.name for c in out.content] == ["run_tests"]
    text = CreateResult(finish_reason="stop", content="hi", usage=RequestUsage(1, 1), cached=False)
    assert repair_create_result(text).content == "hi"


def test_repair_create_result_marks_cut_off_arguments() -> None:
    from autogen_core import FunctionCall
    from autogen_core.models import CreateResult, RequestUsage

    from airlie.teams.autogen_client import repair_create_result

    raw = '{"path": "a.py", "content": "x = 1' + "0" * 500
    res = CreateResult(
        finish_reason="function_calls",
        content=[FunctionCall(id="a", name="write_file", arguments=raw)],
        usage=RequestUsage(1, 1),
        cached=False,
    )
    out = repair_create_result(res)
    args = json.loads(out.content[0].arguments)  # type: ignore[union-attr]
    assert set(args) == {"__truncated__"} and args["__truncated__"].endswith("0" * 20)


def test_already_clean_object_is_returned_byte_for_byte() -> None:
    raw = '{"a":1,  "b":"é"}'
    assert repair_json_args(raw) == raw


def test_repair_keeps_non_ascii_characters() -> None:
    assert repair_json_args('<call>{"s": "héllo ✓"} trailing') == '{"s": "héllo ✓"}'


def test_object_after_a_non_object_prefix_is_not_treated_as_clean() -> None:
    # starts at offset 1, so the leading char must be dropped even though the object ends the text
    assert repair_json_args('x{"a":1}') == '{"a": 1}'


def test_parse_tool_args_empty_means_no_arguments() -> None:
    assert parse_tool_args("") == {}


def test_parse_tool_args_non_object_message_is_exact() -> None:
    with pytest.raises(ValueError) as ei:
        parse_tool_args("[1, 2]")
    assert str(ei.value) == "tool arguments must be a JSON object"
    with pytest.raises(ValueError) as ei:
        parse_tool_args("7")
    assert str(ei.value) == "tool arguments must be a JSON object"
