"""MCP servers from airlie.yaml are exposed to teams as ordinary tools (a configurable seam)."""

from __future__ import annotations

import sys
from pathlib import Path

from airlie.config import MCPServerSpec
from airlie.engine import ToolCall
from airlie.tools import ToolContext, ToolRegistry
from airlie.tools.mcp import MCPProvider

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_echo_server.py"


async def test_mcp_tools_are_listed_and_callable(tmp_path: Path) -> None:
    spec = MCPServerSpec(command=sys.executable, args=[str(SERVER)])
    reg = ToolRegistry.builtin(["read_file"], ToolContext(worktree=tmp_path))
    async with MCPProvider({"echo": spec}) as provider:
        await provider.register_into(reg)
        assert reg.names() == ["read_file", "echo.echo", "echo.add"]
        schema = next(s for s in reg.schemas() if s.name == "echo.add")
        assert schema.parameters["required"] == ["a", "b"]
        r = await reg.call(ToolCall("1", "echo.echo", '{"text": "hi"}'))
        assert r.ok and r.output == "echo: hi"
        r = await reg.call(ToolCall("2", "echo.add", '{"a": 2, "b": 3}'))
        assert r.ok and r.output == "5"


async def test_disabled_server_is_skipped(tmp_path: Path) -> None:
    spec = MCPServerSpec(command=sys.executable, args=[str(SERVER)], enabled=False)
    reg = ToolRegistry.builtin([], ToolContext(worktree=tmp_path))
    async with MCPProvider({"echo": spec}) as provider:
        await provider.register_into(reg)
    assert reg.names() == []


async def test_failed_server_becomes_a_warning_not_a_crash(tmp_path: Path) -> None:
    spec = MCPServerSpec(command="/definitely/not/a/binary", args=[])
    reg = ToolRegistry.builtin([], ToolContext(worktree=tmp_path))
    async with MCPProvider({"bad": spec}) as provider:
        await provider.register_into(reg)
        assert provider.errors and "bad" in provider.errors[0]
    assert reg.names() == []
