"""The file tools in the session's VM: the host tools' names, schemas and results, over `ctx.box`.

There is no host path here: a file's content travels on the box's stdin and stdout (never inside a shell
string), a path is normalised in text against the checkout root in the VM (`ctx.worktree`) and refused when
it leaves it, and no symlink is followed, because inside the VM the boundary is the VM. A chat has none of
the story line's locks (read_only, locked_tests, write_scope)."""

from __future__ import annotations

import posixpath
import re
from typing import Any

from airlie.boxes.protocol import Box
from airlie.subproc import box_env
from airlie.tools import fs
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.sandbox import cap, secret_like

GREP_MATCHES_MAX = 500
SECRET_GLOBS = (".env*", "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.pem", "*.key", "*.p12")
WRITE_SCRIPT = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
"""The path is the script's argument `$1`, so what is in it is never parsed as shell."""


def _box(ctx: ToolContext) -> Box:
    if ctx.box is None:
        raise ValueError("a boxed tool needs a box: ToolContext.box is None")
    return ctx.box


def inside(ctx: ToolContext, path: str) -> str:
    """`path` as an absolute path in the VM under the checkout root, or PermissionError. Lexical: `..` is
    folded away before the check, an absolute path is taken as it is, and a likely secret file is refused."""
    root = posixpath.normpath(str(ctx.worktree))
    candidate = posixpath.normpath(posixpath.join(root, path))
    # the name is judged after normalising: `.env/` and `.env/.` are the file `.env`
    if secret_like(posixpath.basename(candidate)):
        raise PermissionError(f"refusing to touch a likely secret file: {path}")
    if candidate != root and not candidate.startswith(root.rstrip("/") + "/"):
        raise PermissionError(f"path escapes the worktree: {path}")
    return candidate


async def _read(ctx: ToolContext, path: str) -> str:
    """The text of the file at `path` (already inside), or an OSError saying what `cat` said."""
    rc, data = await _box(ctx).exec_bytes(["cat", path], timeout_s=ctx.shell_timeout_s)
    if rc != 0:
        raise OSError(data.decode("utf-8", "replace").strip() or f"cannot read {path}")
    return data.decode("utf-8", "replace")


async def _write(ctx: ToolContext, path: str, content: str) -> None:
    result = await _box(ctx).exec(
        ["sh", "-c", WRITE_SCRIPT, "sh", path],
        env=box_env(ctx.box_env),
        cwd=str(ctx.worktree),
        stdin=content.encode(),
        timeout_s=ctx.shell_timeout_s,
    )
    if result.rc != 0:
        raise OSError(result.output.strip() or f"cannot write {path}")


class BoxReadFile:
    name = fs.ReadFile.name
    description = fs.ReadFile.description
    parameters = fs.ReadFile.parameters

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        has_path, has_paths = args.get("path") is not None, args.get("paths") is not None
        if has_path == has_paths:
            example = "" if has_path else f" {fs.READ_EXAMPLE}"
            return ToolResult(
                False, f"error: read_file needs exactly one of path (one file) or paths (several).{example}"
            )
        if has_paths:
            return await self._run_paths(args, ctx)
        try:
            text = await _read(ctx, inside(ctx, str(args["path"])))
            offset = fs._line_arg(args, "offset") or 1
            limit = fs._line_arg(args, "limit")
        except Exception as e:  # noqa: BLE001
            return fs._err(e)
        lines = text.splitlines()
        if offset > len(lines):
            return ToolResult(True, f"offset {offset} is past the end: {args['path']} has {len(lines)} lines")
        chosen = lines[offset - 1 : offset - 1 + limit] if limit else lines[offset - 1 :]
        numbered = [f"{offset + i}: {ln}" for i, ln in enumerate(chosen)]
        return ToolResult(True, fs._cap_lines(numbered, offset, len(lines), ctx.output_cap_chars))

    async def _run_paths(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if fs._line_arg_given(args):
            return ToolResult(False, "error: offset and limit read one file: use path")
        try:
            paths = fs.paths_arg(args["paths"])
        except ValueError as e:
            return fs._err(e)
        sections: list[str] = []
        room = ctx.output_cap_chars
        for path in paths:
            section = await self._section(ctx, path, room)
            sections.append(section)
            room -= len(section) + 1
        return ToolResult(True, "\n".join(sections))

    async def _section(self, ctx: ToolContext, path: str, room: int) -> str:
        try:
            lines = (await _read(ctx, inside(ctx, path))).splitlines()
        except Exception as e:  # noqa: BLE001
            return f"=== {path} ===\nerror: {e}"
        header = f"=== {path} ({len(lines)} lines) ==="
        numbered = [f"{i + 1}: {ln}" for i, ln in enumerate(lines)]
        body_room = room - len(header) - 1
        if numbered and body_room < len(numbered[0]):
            return f"{path}: not shown, over the output cap; read it with path"
        return f"{header}\n{fs._cap_lines(numbered, 1, len(lines), body_room)}"


async def _too_long_to_rewrite(ctx: ToolContext, path: str, shown: str) -> str | None:
    """The error that refuses overwriting the file at `path` when it is longer than the context's limit and
    was in the commit before this session's work (as the host tool: a file made this session is its own to
    rewrite); None otherwise. A repository with no such commit counts as committed, as on the host."""
    box, env = _box(ctx), box_env(ctx.box_env)
    try:
        n = len((await _read(ctx, path)).splitlines())
    except OSError:  # nothing there yet
        return None
    limit = ctx.write_file_max_overwrite_lines
    if n <= limit:
        return None
    ref = ctx.scope_base or fs.HEAD

    async def vcs_ok(*argv: str) -> bool:
        result = await box.exec(
            [fs.GIT, *argv], env=env, cwd=str(ctx.worktree), timeout_s=ctx.shell_timeout_s
        )
        return result.rc == 0

    if await vcs_ok("rev-parse", f"{ref}^{{commit}}"):
        rel = posixpath.relpath(path, posixpath.normpath(str(ctx.worktree)))
        if not await vcs_ok("cat-file", "-e", f"{ref}:{rel}"):
            return None
    return (
        f"error: {shown} already exists with {n} lines (over {limit}); write_file does not rewrite a file "
        "that long. Change it with edit_file, or put new tests in a new file named for the feature."
    )


class BoxWriteFile:
    name = fs.WriteFile.name
    distinct_by_arguments = True
    description = fs.WriteFile.description
    parameters = fs.WriteFile.parameters

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        content = str(args["content"])
        if ctx.write_max_chars is not None and len(content) > ctx.write_max_chars:
            too_long = fs.WRITE_TOO_LONG.format(path=args["path"], n=len(content), limit=ctx.write_max_chars)
            return ToolResult(False, too_long)
        try:
            path = inside(ctx, str(args["path"]))
            if refused := await _too_long_to_rewrite(ctx, path, str(args["path"])):
                return ToolResult(False, refused)
            await _write(ctx, path, content)
        except Exception as e:  # noqa: BLE001
            return fs._err(e)
        return ToolResult(True, f"wrote {args['path']} ({len(str(args['content']))} chars)")


class BoxEditFile:
    name = fs.EditFile.name
    distinct_by_arguments = True
    description = fs.EditFile.description
    parameters = fs.EditFile.parameters

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        shown, old, new = str(args["path"]), str(args["old"]), str(args["new"])
        if ctx.write_max_chars is not None and len(new) > ctx.write_max_chars:
            return ToolResult(
                False, fs.EDIT_TOO_LONG.format(path=shown, n=len(new), limit=ctx.write_max_chars)
            )
        try:
            path = inside(ctx, shown)
            text = await _read(ctx, path)
        except Exception as e:  # noqa: BLE001
            return fs._err(e)
        n = text.count(old)
        note = ""
        if n == 0:
            span = fs._loose_span(text, old)
            if span is None:
                return ToolResult(False, fs._not_found(shown, old, text))
            edited = text[: span[0]] + new + text[span[1] :]
            note = (
                " (`old` matched with its blank lines and trailing spaces ignored; "
                "quote the file exactly next time)"
            )
        elif n > 1:
            return ToolResult(False, f"error: `old` text occurs {n} times in {shown}; make it unique")
        else:
            edited = text.replace(old, new, 1)
        try:
            await _write(ctx, path, edited)
        except Exception as e:  # noqa: BLE001
            return fs._err(e)
        return ToolResult(True, f"edited {shown}{note}")


def _skipped(root: str, path: str) -> bool:
    """Whether `path` (under `root`) is in a build or VCS directory, or is a likely secret file."""
    parts = set(posixpath.relpath(path, root).split("/"))
    return bool(parts & fs.SKIP_DIRS) or secret_like(posixpath.basename(path))


class BoxListDir:
    name = fs.ListDir.name
    description = fs.ListDir.description
    parameters = fs.ListDir.parameters

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        box, env = _box(ctx), box_env(ctx.box_env)
        try:
            root = inside(ctx, str(args.get("path") or "."))
            is_dir = (await box.exec(["test", "-d", root], env=env, timeout_s=ctx.shell_timeout_s)).rc == 0
        except Exception as e:  # noqa: BLE001
            return fs._err(e)
        if not is_dir:
            return ToolResult(False, f"error: not a directory: {args.get('path')}")
        prune = ["("]
        for i, name in enumerate(sorted(fs.SKIP_DIRS)):
            prune += (["-o"] if i else []) + ["-name", name]
        found = await box.exec(
            ["find", root, *prune, ")", "-prune", "-o", "-type", "f", "-exec", "wc", "-c", "{}", "+"],
            env=env,
            cwd=str(ctx.worktree),
            timeout_s=ctx.shell_timeout_s,
        )
        lines = []
        for line in found.output.splitlines():
            size, _, path = line.strip().partition(" ")
            path = path.strip()
            if not size.isdigit() or not path.startswith(root) or _skipped(root, path):
                continue  # `wc` also prints a "total" line
            lines.append(f"{posixpath.relpath(path, root)} ({size} bytes)")
        return ToolResult(True, cap("\n".join(sorted(lines)) or "(empty)", ctx.output_cap_chars))


class BoxGrep:
    name = fs.Grep.name
    description = fs.Grep.description
    parameters = fs.Grep.parameters

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            pattern = str(args["pattern"])
            re.compile(pattern)  # as the host tool: a pattern that is not a regular expression is refused
            root = inside(ctx, str(args.get("path") or "."))
        except Exception as e:  # noqa: BLE001
            return fs._err(e)
        argv = ["grep", "-rnP"]
        argv += [f"--exclude-dir={d}" for d in sorted(fs.SKIP_DIRS)]
        argv += [f"--exclude={g}" for g in SECRET_GLOBS]
        found = await _box(ctx).exec(
            [*argv, "-e", pattern, "--", root],
            env=box_env(ctx.box_env),
            cwd=str(ctx.worktree),
            timeout_s=ctx.shell_timeout_s,
        )
        if found.rc is None or found.rc > 1:
            return ToolResult(False, f"error: {found.output.strip() or 'grep failed'}")
        base = posixpath.normpath(str(ctx.worktree))
        out: list[str] = []
        last = ""
        for line in found.output.splitlines():
            m = re.match(r"(.+?):(\d+):(.*)", line)
            if m is None or _skipped(base, m.group(1)):
                continue
            if len(out) > GREP_MATCHES_MAX and m.group(1) != last:
                break  # the file that crossed the cap is shown whole, as on the host
            last = m.group(1)
            out.append(f"{posixpath.relpath(m.group(1), base)}:{m.group(2)}: {m.group(3).strip()}")
        return ToolResult(True, cap("\n".join(out) or "(no matches)", ctx.output_cap_chars))
