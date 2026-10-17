"""repair_call: a `<function=NAME>` block the server parser could not read becomes a call when unambiguous."""

from __future__ import annotations

import json

import pytest

from airlie.engine.types import ToolSchema
from airlie.teams.call_repair import repair_call
from airlie.teams.native_team import MALFORMED
from airlie.tools.fs import ReadFile, WriteFile
from airlie.tools.shell import Git

TOOLS = [ToolSchema(t.name, t.description, t.parameters) for t in (ReadFile(), WriteFile(), Git())]

HDS5 = "<function=read_file>\n<file>src/airlie/config/schema.py</file>\n</function>"
"""HDS-5 layer 01 (2026-10-16): a child tag named after the parameter, no `<tool_call>` wrapper."""


def args(text: str) -> dict[str, object]:
    r = repair_call(text, TOOLS)
    assert r.call is not None, r.note
    return dict(json.loads(r.call.arguments))


def test_a_child_tag_named_like_an_alias_maps_onto_the_real_parameter() -> None:
    r = repair_call(HDS5, TOOLS)
    assert r.call is not None and r.call.name == "read_file"
    assert json.loads(r.call.arguments) == {"path": "src/airlie/config/schema.py"}


def test_the_proper_parameter_shape_is_read_too() -> None:
    text = (
        "<tool_call>\n<function=read_file>\n<parameter=path>\nsrc/a.py\n</parameter>\n"
        "</function>\n</tool_call>"
    )
    assert args(text) == {"path": "src/a.py"}


@pytest.mark.parametrize("key", ["file", "filename", "filepath", "file_path"])
def test_file_aliases_mean_path(key: str) -> None:
    assert args(f"<function=read_file><{key}>a.py</{key}></function>") == {"path": "a.py"}


def test_a_multi_line_value_keeps_its_inner_newlines() -> None:
    text = "<function=write_file>\n<path>a.py</path>\n<content>\nx = 1\ny = 2\n</content>\n</function>"
    assert args(text) == {"path": "a.py", "content": "x = 1\ny = 2"}


def test_a_value_that_is_not_a_string_parameter_is_read_as_json() -> None:
    assert args('<function=git><args>["status", "--short"]</args></function>') == {
        "args": ["status", "--short"]
    }


def test_prose_around_the_block_is_kept_apart_from_the_call() -> None:
    r = repair_call(f"Let me read the schema.\n{HDS5}", TOOLS)
    assert r.call is not None and r.prose == "Let me read the schema."


def test_an_unknown_tool_is_never_run() -> None:
    r = repair_call("<function=rm_rf><path>/</path></function>", TOOLS)
    assert r.call is None and "unknown tool" in r.note


def test_a_missing_required_parameter_is_no_call() -> None:
    r = repair_call("<function=write_file><path>a.py</path></function>", TOOLS)
    assert r.call is None and "content" in r.note


def test_two_blocks_are_no_call() -> None:
    r = repair_call(HDS5 + "\n" + HDS5, TOOLS)
    assert r.call is None and "2" in r.note


def test_a_key_that_maps_onto_no_parameter_is_no_call() -> None:
    r = repair_call("<function=read_file><path>a.py</path><mode>r</mode></function>", TOOLS)
    assert r.call is None and "mode" in r.note


def test_two_keys_for_one_parameter_are_no_call() -> None:
    r = repair_call("<function=read_file><path>a.py</path><file>b.py</file></function>", TOOLS)
    assert r.call is None and "path" in r.note


def test_text_inside_the_block_that_is_not_a_child_tag_is_no_call() -> None:
    r = repair_call("<function=read_file>\nread a.py please\n<path>a.py</path>\n</function>", TOOLS)
    assert r.call is None


def test_a_block_with_no_end_is_no_call() -> None:
    r = repair_call("<function=read_file>\n<path>a.py</path>\n", TOOLS)
    assert r.call is None


def test_the_example_in_the_correction_message_is_a_call() -> None:
    example = MALFORMED[
        MALFORMED.index("<tool_call>") : MALFORMED.index("</tool_call>") + len("</tool_call>")
    ]
    r = repair_call(example, TOOLS)
    assert r.call is not None and r.call.name == "read_file"
