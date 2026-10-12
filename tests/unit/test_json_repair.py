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


def test_repair_create_result_fixes_calls_and_dedupes_duplicates() -> None:
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
    assert [c.id for c in out.content] == ["a", "c"]  # type: ignore[union-attr]
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
