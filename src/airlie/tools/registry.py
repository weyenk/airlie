"""ToolRegistry: name → Tool, built from config; executes ToolCalls into observations (never exceptions)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from airlie.engine.types import ToolCall, ToolSchema
from airlie.tools.fs import EditFile, Grep, ListDir, ReadFile, WriteFile
from airlie.tools.jsonargs import parse_tool_args
from airlie.tools.protocol import Tool, ToolContext, ToolResult
from airlie.tools.shell import Git, RunTests, Shell

BUILTIN: dict[str, type] = {
    "read_file": ReadFile,
    "write_file": WriteFile,
    "edit_file": EditFile,
    "list_dir": ListDir,
    "grep": Grep,
    "shell": Shell,
    "run_tests": RunTests,
    "git": Git,
}


_EXAMPLE_VALUES: dict[str, Any] = {"path": "src/app.py", "cmd": "ls"}
_EXAMPLE_BY_TYPE: dict[str, Any] = {"array": ["..."], "integer": 1, "number": 1, "boolean": True}


def example_call(tool: Tool) -> str:
    """One correct call of `tool`, its required arguments filled in from its schema:
    `read_file(path="src/app.py")`."""
    props: dict[str, Any] = tool.parameters.get("properties", {})
    args = []
    for n in (str(n) for n in tool.parameters.get("required", [])):
        typ = str(props.get(n, {}).get("type", "string"))
        value = _EXAMPLE_VALUES.get(n, "...") if typ == "string" else _EXAMPLE_BY_TYPE.get(typ, "...")
        args.append(f"{n}={json.dumps(value)}")
    return f"{tool.name}({', '.join(args)})"


def missing_args_error(tool: Tool, missing: Iterable[str]) -> str:
    """HDS-5 2026-10-16: a model sending empty calls needs to see a correct one, not only what is missing."""
    return (
        f"error: {tool.name} requires: {', '.join(missing)}. A correct call looks like: {example_call(tool)}"
    )


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool], ctx: ToolContext) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}
        self.ctx = ctx

    @classmethod
    def builtin(cls, names: Iterable[str], ctx: ToolContext) -> ToolRegistry:
        tools: list[Tool] = []
        for n in names:
            if n not in BUILTIN:
                raise KeyError(f"unknown builtin tool '{n}' (known: {sorted(BUILTIN)})")
            tools.append(BUILTIN[n]())
        return cls(tools, ctx)

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[ToolSchema]:
        return [ToolSchema(t.name, t.description, t.parameters) for t in self._tools.values()]

    async def call(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            if not self._tools:
                return ToolResult(
                    False, f"error: unknown tool '{call.name}': this role has no tools; answer directly"
                )
            return ToolResult(
                False, f"error: unknown tool '{call.name}'; available: {', '.join(self._tools)}"
            )
        try:
            args = parse_tool_args(call.arguments)
        except ValueError as e:
            if "could not parse" in str(e) and len(call.arguments) > 40:  # cut off mid-string: a runaway
                return ToolResult(
                    False,
                    f"error: the arguments of {call.name} were cut off (the generation ran away, ending in "
                    f"{call.arguments[-4:]!r}). Make a shorter call: write a smaller file or split the "
                    "change.",
                )
            return ToolResult(False, f"error: {e}; arguments were: {call.arguments[:200]}")
        required = [str(n) for n in tool.parameters.get("required", [])]
        if missing := [n for n in required if args.get(n) is None]:
            return ToolResult(False, missing_args_error(tool, missing))
        try:
            return await tool.run(args, self.ctx)
        except Exception as e:  # noqa: BLE001 - a tool bug becomes an observation, never a crash
            return ToolResult(False, f"error: tool '{call.name}' failed: {type(e).__name__}: {e}")
