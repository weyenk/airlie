"""Builtin file tools: read_file, write_file, edit_file, list_dir, grep."""

from __future__ import annotations

import difflib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.sandbox import cap, resolve, secret_like
from airlie.tools.test_lock import touched

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


def _line_arg(args: dict[str, Any], name: str) -> int | None:
    """An optional 1-based line argument, given as a number or a numeric string (models send both); None when
    it is not given (absent, or empty)."""
    raw = args.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{name} must be a whole number of lines, not {raw!r}") from None
    return max(1, value)


def _line_arg_given(args: dict[str, Any]) -> bool:
    """Whether offset or limit is given (not absent, not empty)."""
    return any(args.get(n) not in (None, "") for n in ("offset", "limit"))


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


READ_PATHS_MAX = 5
"""How many files one read_file call takes: each tool call costs a role one step, and the whole result stays
under the output cap of a single read, so batching never adds context per step."""


def paths_arg(raw: Any) -> list[str]:
    """The `paths` argument as a list of paths: a list, or (some models send it so) a string holding a JSON
    list. Raises ValueError for anything else, and for more than READ_PATHS_MAX."""
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            value = None  # pragma: no mutate  (any value that is not a list is refused the same)
    if not isinstance(value, list) or not value:
        raise ValueError('paths must be a list of paths, e.g. ["a.py", "b.py"]')
    if len(value) > READ_PATHS_MAX:
        raise ValueError(
            f"paths takes at most {READ_PATHS_MAX} files, not {len(value)}: read the rest in another call"
        )
    return [str(v) for v in value]


def _read_section(ctx: ToolContext, path: str, room: int) -> str:
    """One file of a multi-file read under its `=== path (n lines) ===` header, cut to `room` chars with the
    same notice `_cap_lines` gives a single read. A file with no room for even its first line is listed, not
    shown, so the model reads it alone. A file that cannot be read shows its error under the header."""
    try:
        lines = resolve(ctx, path).read_text(errors="replace").splitlines()
    except Exception as e:  # noqa: BLE001
        return f"=== {path} ===\nerror: {e}"
    header = f"=== {path} ({len(lines)} lines) ==="
    numbered = [f"{i + 1}: {ln}" for i, ln in enumerate(lines)]
    body_room = room - len(header) - 1
    if numbered and body_room < len(numbered[0]):
        return f"{path}: not shown, over the output cap; read it with path"
    return f"{header}\n{_cap_lines(numbered, 1, len(lines), body_room)}"


READ_EXAMPLE = 'A correct call looks like: read_file(path="src/app.py")'
"""Shown when read_file gets neither argument: HDS-5's model sent it empty until it saw a worked call."""


class ReadFile:
    name = "read_file"
    description = (
        "Read a text file from the repository. Returns its contents with line numbers. A long file is cut at "
        "the output cap: read on with offset (the first line to show, 1-based) and limit (how many lines). "
        f"Several small files at once: paths (up to {READ_PATHS_MAX}), each shown in full up to the output "
        "cap."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "first line to show, 1-based (default 1)"},
            "limit": {"type": "integer", "description": "how many lines to show (default: to the end)"},
            "paths": {"type": "array", "items": {"type": "string"}, "maxItems": READ_PATHS_MAX},
        },
        "required": [],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        has_path, has_paths = args.get("path") is not None, args.get("paths") is not None
        if has_path == has_paths:
            # with `required` empty the registry's missing-argument error, and its worked call, never fires
            example = "" if has_path else f" {READ_EXAMPLE}"
            return ToolResult(
                False, f"error: read_file needs exactly one of path (one file) or paths (several).{example}"
            )
        if has_paths:
            return self._run_paths(args, ctx)
        try:
            p = resolve(ctx, str(args["path"]))
            text = p.read_text(errors="replace")
            offset = _line_arg(args, "offset") or 1
            limit = _line_arg(args, "limit")
        except Exception as e:  # noqa: BLE001
            return _err(e)
        lines = text.splitlines()
        if offset > len(lines):
            return ToolResult(True, f"offset {offset} is past the end: {args['path']} has {len(lines)} lines")
        chosen = lines[offset - 1 : offset - 1 + limit] if limit else lines[offset - 1 :]
        numbered = [f"{offset + i}: {ln}" for i, ln in enumerate(chosen)]
        return ToolResult(True, _cap_lines(numbered, offset, len(lines), ctx.output_cap_chars))

    def _run_paths(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Several files, each under its header, the whole under the output cap: a file takes what room is
        left, so the first files are shown whole and the last are cut or listed as not shown."""
        if _line_arg_given(args):
            return ToolResult(False, "error: offset and limit read one file: use path")
        try:
            paths = paths_arg(args["paths"])
        except ValueError as e:
            return _err(e)
        sections: list[str] = []
        room = ctx.output_cap_chars
        for path in paths:
            section = _read_section(ctx, path, room)
            sections.append(section)
            room -= len(section) + 1
        return ToolResult(True, "\n".join(sections))


HEAD = "HEAD"
GIT = "git"
"""The worktree's commit when a run has no base, and the program that reads it. Constants, not literals in
the calls: on macOS's case-insensitive disk git reads "head" as HEAD and finds GIT as git, so a mutant that
changes their case could not be told apart."""


def _git_ok(worktree: Path, *args: str) -> bool:
    """Whether `git -C worktree <args>` succeeds. What it prints is kept from the tool's own output."""
    return subprocess.run([GIT, "-C", str(worktree), *args], capture_output=True).returncode == 0


def _at_base(worktree: Path, p: Path, ref: str) -> bool:
    """Whether `p` is in the worktree's commit `ref` (the run's base, else HEAD): code that was there before
    the run, which the overwrite limit protects. A file the coder created this run is its own to rewrite,
    even once a round has committed it (the review and a green run_tests commit as they go). No git, or no
    such commit (a repository with nothing committed yet): True. `p` comes from `resolve`, in the worktree.
    Asked of git, not read from its error text: git 2.4x says "invalid object name" where older ones said
    "Not a valid object name", and a repository with no commit lost the limit (PR 105's mutation gate)."""
    if not _git_ok(worktree, "rev-parse", f"{ref}^{{commit}}"):  # only its exit code is read
        return True
    rel = p.resolve().relative_to(worktree.resolve()).as_posix()
    return _git_ok(worktree, "cat-file", "-e", f"{ref}:{rel}")


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


LOCKED_TESTS = (
    "error: this change touches {names} in {path}: tests written before you for this layer, your contract, "
    "and you cannot change them. Make them pass with the code. What else is in the file was there before "
    "them (fakes, helpers, imports): you may change that."
)
UNPARSEABLE = (
    "error: {path} would not parse after this change, so airlie cannot tell that the tests written before "
    "you are untouched. Change it in steps that each leave it whole."
)


def _refused_by_definition(ctx: ToolContext, p: Path, shown: str, before: str, after: str) -> str | None:
    """The error that refuses changing `before` to `after` in `p` when that touches the tester's definitions
    in it (`ctx.locked_tests`; AIR-9 layer 05, 2026-10-22: a whole-file lock kept the coder from fixing the
    fakes beside the tests); None when `p` holds none of them or the change leaves them be. `p` comes from
    `resolve`, in the worktree."""
    names = ctx.locked_tests.get(p.resolve().relative_to(ctx.worktree.resolve()).as_posix())
    if not names:
        return None
    hit = touched(names, before, after)
    if hit is None:
        return UNPARSEABLE.format(path=shown)
    return LOCKED_TESTS.format(path=shown, names=", ".join(hit)) if hit else None


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
            if p.is_file() and (
                refused := _refused_by_definition(
                    ctx, p, str(args["path"]), p.read_text(errors="replace"), str(args["content"])
                )
            ):
                return ToolResult(False, refused)
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


def _closest(line: str, lines: list[str]) -> str | None:
    """The line of `lines` closest to `line` (at least 0.6 alike), or None."""
    # n=1 only caps a list whose first item is the answer, and 0.6 is difflib's default: neither changes it
    found = difflib.get_close_matches(line, lines, n=1, cutoff=0.6)  # pragma: no mutate
    return found[0] if found else None


def _not_found(path: str, old: str, text: str) -> str:
    """The edit_file error for an `old` that is not in the file: the line that comes closest to its first
    line, and how to see it. Every failed edit of 2026-10-18 (AIR-7) was an anchor quoted from memory."""
    # a blank `old` has no first line: any default that is no line of the file reads the same
    first = next((line for line in old.splitlines() if line.strip()), "")  # pragma: no mutate
    lines = text.splitlines()
    close = _closest(first, lines) if first and lines else None
    if close == first:  # the anchor is there: say where the quote goes wrong below it
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
    if close is not None:
        return (
            f"error: `old` text not found in {path}. The closest line is {lines.index(close) + 1}: "
            f"`{close}`. Read the file there (read_file with offset and limit) and quote it exactly, "
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
        for i in range(len(have))
        if [h[0] for h in have[i : i + k]] == want  # a slice past the end is short: never `want`
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
            edited = text[: span[0]] + new + text[span[1] :]
            if refused := _refused_by_definition(ctx, p, str(args["path"]), text, edited):
                return ToolResult(False, refused)
            p.write_text(edited)
            return ToolResult(
                True,
                f"edited {args['path']} (`old` matched with its blank lines and trailing spaces ignored; "
                "quote the file exactly next time)",
            )
        if n > 1:
            return ToolResult(False, f"error: `old` text occurs {n} times in {args['path']}; make it unique")
        edited = text.replace(old, new, 1)  # pragma: no mutate  (n == 1 here, so the count is moot)
        if refused := _refused_by_definition(ctx, p, str(args["path"]), text, edited):
            return ToolResult(False, refused)
        p.write_text(edited)
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
