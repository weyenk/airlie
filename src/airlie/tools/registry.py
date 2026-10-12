"""ToolRegistry: name → Tool, built from config; executes ToolCalls into observations (never exceptions)."""

from __future__ import annotations

from collections.abc import Iterable

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
            return ToolResult(
                False, f"error: unknown tool '{call.name}'; available: {', '.join(self._tools)}"
            )
        try:
            args = parse_tool_args(call.arguments)
        except ValueError as e:
            return ToolResult(False, f"error: {e}; arguments were: {call.arguments[:200]}")
        try:
            return await tool.run(args, self.ctx)
        except Exception as e:  # noqa: BLE001 - a tool bug becomes an observation, never a crash
            return ToolResult(False, f"error: tool '{call.name}' failed: {type(e).__name__}: {e}")
