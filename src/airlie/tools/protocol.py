"""Tool seam: what a team can do to the world. Builtins are sandboxed to the worktree; MCP tools wrap."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from airlie.tools.scope import GateMemo


@dataclass(frozen=True)
class ToolContext:
    worktree: Path
    shell_timeout_s: float = 120.0
    output_cap_chars: int = 20000
    test_cmd: str | None = None
    test_steps: tuple[str, ...] = ()
    """The gate's commands, one by one: run_tests runs each and reports each (`test_cmd` is them joined, as
    the final gate runs them). Empty: run_tests runs `test_cmd`."""
    scope_base: str = ""
    """A git commit: when set, run_tests runs the gate's pytest on the test files changed since it, and the
    whole gate only once those pass (tools.scope). Empty: run_tests runs the whole gate every time."""
    gate_memo: GateMemo | None = None
    """Shared with the studio's final gate: the tree that last passed the whole gate need not run it again."""
    write_file_max_overwrite_lines: int = 200
    """write_file refuses to replace an existing file longer than this: such a change goes through edit_file,
    or new tests go in a new file (HDS-5 2026-10-16: a whole-file rewrite ran past the coder's max_tokens)."""
    run_dir: Path | None = None
    """The run's directory: when set, tools that log (web fetches, searches) write their records there."""
    role: str = ""
    """The role this context serves: tools that log or guard by role (web tools) read it from here."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...
