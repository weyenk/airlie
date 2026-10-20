"""Builtin file tools: read_file, write_file, edit_file, list_dir, grep."""

from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path
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


HEAD = "HEAD"
"""The worktree's commit when a run has no base. A constant, not a literal in the call: on macOS's
case-insensitive disk git reads "head" as HEAD too, so a mutant that lowercases it could not be told apart."""


def _at_base(worktree: Path, p: Path, ref: str) -> bool:
    """Whether `p` is in the worktree's commit `ref` (the run's base, else HEAD): code that was there before
    the run, which the overwrite limit protects. A file the coder created this run is its own to rewrite,
    even once a round has committed it (the review and a green run_tests commit as they go). No git, or no
    such commit: True."""
    try:
        rel = p.resolve().relative_to(worktree.resolve()).as_posix()
    except ValueError:
        return True
    done = subprocess.run(
        ["git", "-C", str(worktree), "cat-file", "-e", f"{ref}:{rel}"], capture_output=True, check=False
    )
    unknown = b"Not a valid object name" in done.stderr or b"not a git repository" in done.stderr
    return done.returncode == 0 or unknown


LOCKED = (
    "error: {path} holds this layer's acceptance tests, written before you: they are your contract and you "
    "cannot change them. Make them pass with the code. If one is wrong about a name or a signature the brief "
    "fixes, say which and why when you stop."
)


def _locked(ctx: ToolContext, p: Path) -> bool:
    """Whether `p` is one of `ctx.read_only` (the tester's test files, for the coder). `p` comes from
    `resolve`, which refuses a path outside the worktree."""
    if not ctx.read_only:
        return False
    return p.resolve().relative_to(ctx.worktree.resolve()).as_posix() in ctx.read_only


class WriteFile:
    name = "write_file"
    description = (
        "Create a text file in the repository with the given content, or overwrite one of at most "
        f"{ToolContext.write_file_max_overwrite_lines} lines, or any file you created in this run. A longer "
        "file that was there before is refused unread: change it with edit_file, or put new content in a "
        "new file."
    )
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            p = resolve(ctx, str(args["path"]))
            if _locked(ctx, p):
                return ToolResult(False, LOCKED.format(path=args["path"]))
            if (
                p.is_file()
                and (n := len(p.read_text(errors="replace").splitlines()))
                > (limit := ctx.write_file_max_overwrite_lines)
                and _at_base(ctx.worktree, p, ctx.scope_base or HEAD)
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


def _not_found(path: str, old: str, text: str) -> str:
    """The edit_file error for an `old` that is not in the file: the line that comes closest to its first
    line, and how to see it. Every failed edit of 2026-10-18 (AIR-7) was an anchor quoted from memory."""
    first = next((line for line in old.splitlines() if line.strip()), "")
    lines = text.splitlines()
    close = difflib.get_close_matches(first, lines, n=1, cutoff=0.6) if first and lines else []
    if close and close[0] == first:  # the anchor is there: say where the quote goes wrong below it
        at = lines.index(first)
        wanted = old.splitlines()
        for k, line in enumerate(wanted):
            have = lines[at + k] if at + k < len(lines) else ""
            if line.rstrip() != have.rstrip():  # trailing and whitespace-only differences are not the problem
                return (
                    f"error: `old` text not found in {path}. Its first line is line {at + 1} of the file; "
                    f"the first line of `old` that differs is its line {k + 1}, `{line}`, where the file "
                    f"has line {at + k + 1}: `{have}`. Read the file there (read_file with offset and "
                    "limit) and quote it exactly, whitespace included."
                )
    if close:
        return (
            f"error: `old` text not found in {path}. The closest line is {lines.index(close[0]) + 1}: "
            f"`{close[0]}`. Read the file there (read_file with offset and limit) and quote it exactly, "
            "whitespace included."
        )
    return (
        f"error: `old` text not found in {path}. Read the file (read_file with offset and limit) and quote "
        "the text exactly, whitespace included."
    )


def _loose_span(text: str, old: str) -> tuple[int, int] | None:
    """The one span of `text` that is `old` with blank lines and trailing whitespace ignored, as (start, end)
    offsets; None when there is no such span, or more than one. Indentation still counts. A Qwen2.5 coder
    (AIR-7 layer 03, 2026-10-18) quoted two imports without the blank line between them three times, after
    reading the file: the quote was right in every way a model can see."""
    want = [line.rstrip() for line in old.splitlines() if line.strip()]
    if not want:
        return None
    have: list[tuple[str, int, int, int]] = []  # each non-blank line: text, start, end, end with its newline
    pos = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body.strip():
            have.append((body.rstrip(), pos, pos + len(body), pos + len(line)))
        pos += len(line)
    k = len(want)
    spans = [
        (have[i][1], have[i + k - 1][3] if old.endswith("\n") else have[i + k - 1][2])
        for i in range(len(have) - k + 1)
        if [h[0] for h in have[i : i + k]] == want
    ]
    return spans[0] if len(spans) == 1 else None


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
            if _locked(ctx, p):
                return ToolResult(False, LOCKED.format(path=args["path"]))
            text = p.read_text()
        except Exception as e:  # noqa: BLE001
            return _err(e)
        old, new = str(args["old"]), str(args["new"])
        n = text.count(old)
        if n == 0:
            span = _loose_span(text, old)
            if span is None:
                return ToolResult(False, _not_found(str(args["path"]), old, text))
            p.write_text(text[: span[0]] + new + text[span[1] :])
            return ToolResult(
                True,
                f"edited {args['path']} (`old` matched with its blank lines and trailing spaces ignored; "
                "quote the file exactly next time)",
            )
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
