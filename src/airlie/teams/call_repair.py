"""repair_call: a tool call the server's parser left in the text, read when it is unambiguous.

The server parser (`qwen3_xml_raw`) reads the chat template's format exactly: `<function=NAME>` with
`<parameter=KEY>VALUE</parameter>` children. On HDS-5 (2026-10-16) the coder wrote `<file>…</file>` instead,
a child tag named after the parameter, and the call stayed text. This reads one `<function=NAME>` block whose
children are either form and maps each KEY onto NAME's parameters, exactly or through a short alias table.
It returns a call only when the reply holds one such block, NAME is a tool the role has, every KEY maps onto a
distinct parameter and every required one is filled; anything else stays malformed, with a note saying why."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from airlie.engine.types import ToolCall, ToolSchema
from airlie.tools.jsonargs import typed_value

ALIASES: dict[str, str] = {
    "file": "path",
    "filename": "path",
    "filepath": "path",
    "file_path": "path",
    "command": "cmd",
    "old_str": "old",
    "old_string": "old",
    "new_str": "new",
    "new_string": "new",
}
"""Names models use for the builtin tools' parameters (read_file/write_file/edit_file path, shell cmd,
edit_file old/new). Applied only when the tool has the target parameter and not the key itself."""

_BLOCK = re.compile(r"(?:<tool_call>\s*)?<function=([^>\s]+)>(.*?)</function>(?:\s*</tool_call>)?", re.S)
_START = re.compile(r"<function=")
_CHILD = re.compile(r"\s*<(?:parameter=([^>\s]+)|([A-Za-z_][\w.-]*))>(.*?)</(?:parameter|\2)>\s*", re.S)


@dataclass(frozen=True)
class Repair:
    call: ToolCall | None
    """The call to run, or None: the reply stays malformed."""
    note: str
    """Why there is no call, or which call was read."""
    prose: str = ""
    """The reply's text outside the block."""


def _children(body: str) -> list[tuple[str, str]] | None:
    """(key, value) for each child of a block body; None when the body holds anything else."""
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(body):
        m = _CHILD.match(body, pos)
        if m is None:
            return None if body[pos:].strip() else out
        value = m.group(3).removeprefix("\n").removesuffix("\n")  # one newline each side is format
        out.append((m.group(1) or m.group(2), value))
        pos = m.end()
    return out


def repair_call(text: str, tools: Sequence[ToolSchema]) -> Repair:
    blocks = len(_START.findall(text))
    if blocks != 1:
        return Repair(None, f"{blocks} <function=…> blocks; only a single one is repaired")
    m = _BLOCK.search(text)
    if m is None:
        return Repair(None, "the <function=…> block has no </function>")
    name = m.group(1)
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return Repair(None, f"unknown tool '{name}'")
    children = _children(m.group(2))
    if children is None:
        return Repair(None, f"{name}: the block holds text that is not a <KEY>value</KEY> tag")
    props = tool.parameters.get("properties") or {}
    args: dict[str, object] = {}
    for key, value in children:
        param = key if key in props else ALIASES.get(key)
        if param is None or param not in props:
            return Repair(None, f"{name} has no parameter '{key}'")
        if param in args:
            return Repair(None, f"{name}: two values for '{param}'")
        args[param] = typed_value(value, props[param])
    required = [str(r) for r in tool.parameters.get("required", [])]
    if missing := [r for r in required if r not in args]:
        return Repair(None, f"{name} is missing required: {', '.join(missing)}")
    call = ToolCall(f"call_repaired_{uuid.uuid4().hex[:16]}", name, json.dumps(args, ensure_ascii=False))
    prose = (text[: m.start()] + text[m.end() :]).strip()
    return Repair(call, f"read as {name}({', '.join(args)})", prose)
