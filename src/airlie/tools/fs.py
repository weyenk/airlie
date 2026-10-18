"""Builtin file tools: read_file, write_file, edit_file, list_dir, grep."""

from __future__ import annotations

import re
from typing import Any

from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.sandbox import cap, resolve, secret_like

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "dist",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}


def _err(e: Exception) -> ToolResult:
    return ToolResult(False, f"error: {e}")


def _line_arg(args: dict[str, Any], name: str, default: int) -> int:
    """An optional 1-based line argument, given as a number or a numeric string (models send both)."""
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{name} must be a whole number of lines, not {raw!r}") from None
    return max(1, value)


def _cap_lines(numbered: list[str], first: int, total: int, limit: int) -> str:
    """The numbered lines joined, cut at `limit` chars on a line boundary, with a notice that says which lines
    are missing and the offset that reads on from there (AIR-7, 2026-10-18: the coder re-read one 27k-char
    file five times for its tail, each time getting the same first 20k)."""
    text = "\n".join(numbered)
    if len(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for ln in numbered:
        if used + len(ln) + (1 if kept else 0) > limit:
            break
        used += len(ln) + (1 if kept else 0)
        kept.append(ln)
    shown_upto = first + len(kept) - 1
    missing = len(text) - used
    tail = f"lines {shown_upto + 1}-{total} not shown; read on with offset={shown_upto + 1}"
    return "\n".join(kept) + f"\n… [truncated: {tail}] [truncated {missing} chars]"


class ReadFile:
    name = "read_file"
    description = (
        "Read a text file from the repository. Returns its contents with line numbers. A long file is cut at "
        "the output cap: read on with offset (the first line to show, 1-based) and limit (how many lines)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "first line to show, 1-based (default 1)"},
            "limit": {"type": "integer", "description": "how many lines to show (default: to the end)"},
        },
        "required": ["path"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            p = resolve(ctx, str(args["path"]))
            text = p.read_text(errors="replace")
            offset = _line_arg(args, "offset", 1)
            limit = _line_arg(args, "limit", 0) if args.get("limit") not in (None, "") else 0
        except Exception as e:  # noqa: BLE001
            return _err(e)
        lines = text.splitlines()
        if offset > len(lines):
            return ToolResult(True, f"offset {offset} is past the end: {args['path']} has {len(lines)} lines")
        chosen = lines[offset - 1 : offset - 1 + limit] if limit else lines[offset - 1 :]
        numbered = [f"{offset + i}: {ln}" for i, ln in enumerate(chosen)]
        return ToolResult(True, _cap_lines(numbered, offset, len(lines), ctx.output_cap_chars))


class WriteFile:
    name = "write_file"
    description = (
        "Create a text file in the repository with the given content, or overwrite a short one. "
        "Change a long existing file with edit_file."
    )
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            p = resolve(ctx, str(args["path"]))
            if p.is_file() and (n := len(p.read_text(errors="replace").splitlines())) > (
                limit := ctx.write_file_max_overwrite_lines
            ):
                return ToolResult(
                    False,
                    f"error: {args['path']} already exists with {n} lines (over {limit}); write_file does "
                    "not rewrite a file that long. Change it with edit_file, or put new tests in a new file "
                    "named for the feature.",
                )
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(args["content"]))
        except Exception as e:  # noqa: BLE001
            return _err(e)
        return ToolResult(True, f"wrote {args['path']} ({len(str(args['content']))} chars)")


class EditFile:
    name = "edit_file"
    description = (
        "Replace one exact occurrence of `old` with `new` in a file. Fails if `old` is absent or ambiguous."
    )
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        "required": ["path", "old", "new"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            p = resolve(ctx, str(args["path"]))
            text = p.read_text()
        except Exception as e:  # noqa: BLE001
            return _err(e)
        old, new = str(args["old"]), str(args["new"])
        n = text.count(old)
        if n == 0:
            return ToolResult(False, f"error: `old` text not found in {args['path']}")
        if n > 1:
            return ToolResult(False, f"error: `old` text occurs {n} times in {args['path']}; make it unique")
        p.write_text(text.replace(old, new, 1))  # pragma: no mutate  (n == 1 here, so the count is moot)
        return ToolResult(True, f"edited {args['path']}")


class ListDir:
    name = "list_dir"
    description = "List files under a directory (recursive, skips build/VCS dirs), with sizes."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "default": "."}},
        "required": [],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            root = resolve(ctx, str(args.get("path") or "."))
        except Exception as e:  # noqa: BLE001
            return _err(e)
        if not root.is_dir():
            return ToolResult(False, f"error: not a directory: {args.get('path')}")
        lines = []
        for p in sorted(root.rglob("*")):
            if set(p.relative_to(root).parts) & SKIP_DIRS or secret_like(p.name):
                continue
            if p.is_file():
                lines.append(f"{p.relative_to(root)} ({p.stat().st_size} bytes)")
        return ToolResult(True, cap("\n".join(lines) or "(empty)", ctx.output_cap_chars))


class Grep:
    name = "grep"
    description = "Search files for a regular expression. Returns path:line: text matches."
    parameters = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}},
        "required": ["pattern"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            rx = re.compile(str(args["pattern"]))
            root = resolve(ctx, str(args.get("path") or "."))
        except Exception as e:  # noqa: BLE001
            return _err(e)
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        out = []
        for p in files:
            if set(p.relative_to(ctx.worktree.resolve()).parts) & SKIP_DIRS or secret_like(p.name):
                continue
            try:
                for i, ln in enumerate(p.read_text(errors="replace").splitlines(), 1):
                    if rx.search(ln):
                        out.append(f"{p.relative_to(ctx.worktree.resolve())}:{i}: {ln.strip()}")
            except OSError:
                continue
            if len(out) > 500:
                break
        return ToolResult(True, cap("\n".join(out) or "(no matches)", ctx.output_cap_chars))
