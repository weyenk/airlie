"""Tool seam: what a team can do to the world. Builtins are sandboxed to the worktree; MCP tools wrap."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolContext:
    worktree: Path
    shell_timeout_s: float = 120.0
    output_cap_chars: int = 20000
    test_cmd: str | None = None
    test_steps: tuple[str, ...] = ()
    """The gate's commands, one by one: run_tests runs each and reports each (`test_cmd` is them joined, as
    the final gate runs them). Empty: run_tests runs `test_cmd`."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...
