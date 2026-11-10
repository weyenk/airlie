"""Shared helpers extracted from tests/unit/test_tool_loop.py for tests.support.tool_loop."""

from __future__ import annotations

import json
from typing import Any

from airlie.engine import FakeReply, ToolCall
from airlie.tools.protocol import ToolContext, ToolResult


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


def search(query: str, call_id: str = "c1") -> FakeReply:
    return FakeReply(tool_calls=[ToolCall(call_id, "web_search", json.dumps({"query": query}))])
