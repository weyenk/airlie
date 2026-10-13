"""MCP client: every tool of every configured MCP server becomes an airlie Tool named `<server>.<tool>`."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from airlie.config import MCPServerSpec
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry


class MCPTool:
    def __init__(
        self, server: str, session: ClientSession, name: str, description: str, parameters: dict[str, Any]
    ) -> None:
        self.server = server
        self._session = session
        self._remote = name
        self.name = f"{server}.{name}"
        self.description = description
        self.parameters = parameters

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        res = await self._session.call_tool(self._remote, arguments=args)
        parts: list[str] = []
        for c in res.content:
            text = getattr(c, "text", None)
            if isinstance(text, str):
                parts.append(text)
            else:
                parts.append(json.dumps(getattr(c, "model_dump", dict)(), default=str))
        return ToolResult(
            not bool(getattr(res, "is_error", getattr(res, "isError", False))),
            "\n".join(parts)[: ctx.output_cap_chars],
        )


class MCPProvider:
    """Starts the configured stdio servers for the duration of a run; `errors` lists servers that failed."""

    def __init__(self, servers: dict[str, MCPServerSpec]) -> None:
        self.servers = servers
        self.errors: list[str] = []
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}

    async def __aenter__(self) -> MCPProvider:
        await self._stack.__aenter__()
        for name, spec in self.servers.items():
            if not spec.enabled:
                continue
            try:
                params = StdioServerParameters(
                    command=spec.command, args=list(spec.args), env=dict(spec.env) or None
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
            except Exception as e:  # noqa: BLE001 - a broken MCP server must not take the run down
                self.errors.append(f"mcp server '{name}' failed to start: {type(e).__name__}: {e}")
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._stack.__aexit__(et, ev, tb)

    async def register_into(self, registry: ToolRegistry) -> None:
        for server, session in self._sessions.items():
            listed = await session.list_tools()
            for t in listed.tools:
                registry.add(
                    MCPTool(
                        server,
                        session,
                        t.name,
                        t.description or "",
                        dict(getattr(t, "input_schema", getattr(t, "inputSchema", None)) or {}),
                    )
                )
