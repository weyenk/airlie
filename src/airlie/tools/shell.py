"""Builtin process tools: shell, run_tests, git. All timeboxed, output-capped, run inside the worktree with
the agent environment (airlie.subproc: no credentials) and, on macOS, unable to write outside it
(airlie.tools.confine)."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from airlie.subproc import agent_env, kill_group, started
from airlie.tools import confine
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.sandbox import cap
from airlie.tools.scope import autofix_steps, changed_test_files, scoped_steps

SH = "/bin/sh"
"""The shell a confined command runs in. A constant, not a literal in the call: on macOS's case-insensitive
disk /BIN/SH is the same file, so a mutant that upper-cases it could not be told apart."""
GIT_DENY = {"push", "fetch", "pull", "remote", "clone", "submodule", "gc", "reflog", "worktree", "config"}


async def run_cmd(cmd: list[str] | str, ctx: ToolContext, *, shell: bool) -> ToolResult:
    """Run a command a model chose, or code a model wrote, in the worktree: with the agent environment, no
    stdin, confined to writing where its work is (ctx.sandbox), and in a process group of its own, so a
    timeout or the caller being cancelled kills the command and everything it started."""
    rc, text = await _run(cmd, ctx, shell=shell)
    if rc is None:
        return ToolResult(False, text)
    body = cap(text, ctx.output_cap_chars)
    if rc != 0:
        body += f"\n[exit {rc}]"
    return ToolResult(rc == 0, body)


async def _run(
    cmd: list[str] | str, ctx: ToolContext, *, shell: bool, cwd: Path | None = None
) -> tuple[int | None, str]:
    """`run_cmd`'s work: (the exit code, the whole output), or (None, why) when the command could not start or
    timed out. It runs in `cwd` (default: the worktree); what it may write is set by the worktree."""
    env = agent_env(ctx.worktree)
    confined = ctx.sandbox and confine.available()
    spawn: dict[str, Any] = dict(
        cwd=cwd or ctx.worktree,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,  # never the person's terminal
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # its own process group: kill_group reaches what it starts
    )
    try:
        if confined:
            command = [SH, "-c", str(cmd)] if shell else [str(c) for c in cmd]
            if not shell and shutil.which(command[0], path=env.get("PATH")) is None:
                # as an unconfined exec fails (sandbox-exec would say it with exit 71 instead)
                raise FileNotFoundError(2, "No such file or directory", command[0])
            roots = confine.writable_roots(ctx.worktree, ctx.writable)
            proc = await started(asyncio.create_subprocess_exec(*confine.argv(command, roots), **spawn))
        elif shell:
            proc = await started(asyncio.create_subprocess_shell(str(cmd), **spawn))
        else:
            proc = await started(asyncio.create_subprocess_exec(*cmd, **spawn))
    except OSError as e:
        return None, f"error: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=ctx.shell_timeout_s)
    except TimeoutError:
        await kill_group(proc)
        return None, f"error: command timed out after {ctx.shell_timeout_s:g}s"
    except BaseException:  # cancelled: nothing the command started may outlive its caller
        await kill_group(proc)
        raise
    text = out.decode("utf-8", "replace")  # pragma: no mutate  (codec names are case-insensitive)
    rc = proc.returncode or 0
    return rc, confine.explain(text, rc) if confined else text


async def run_steps(steps: Sequence[str], ctx: ToolContext) -> ToolResult:
    """Run each of a gate's commands in turn, going on after one fails, and report each: PASS with its last
    line, or FAIL with its output, the failing ones sharing ctx.output_cap_chars. Each runs in sh as the
    gate runs it (in a group of its own, so an inline # comment ends at its line) with ctx.shell_timeout_s,
    from the directory the one before left: a `cd` carries over, as it does in the gate."""
    cwd = ctx.worktree
    ran: list[tuple[str, int | None, str]] = []
    with tempfile.TemporaryDirectory(prefix="airlie-run-tests-") as tmp:
        where = Path(tmp) / "cwd"
        for step in steps:
            script = f"{{ {step}\n}}\nrc=$?\npwd > {shlex.quote(str(where))}\nexit $rc"
            where.unlink(missing_ok=True)
            rc, text = await _run(script, ctx, shell=True, cwd=cwd)
            ran.append((step, rc, text))
            if where.is_file() and Path(moved := where.read_text().strip()).is_dir():
                cwd = Path(moved)
    failed = [i for i, (_, rc, _) in enumerate(ran, 1) if rc != 0]
    share = ctx.output_cap_chars // max(len(failed), 1)
    n = len(ran)
    lines = [f"{n - len(failed)} of {n} gate commands pass"]
    if failed:
        lines[0] += "; failed: " + ", ".join(f"[{i}/{n}]" for i in failed)
    for i, (step, rc, text) in enumerate(ran, 1):
        if rc == 0:
            last = next((x for x in reversed(text.splitlines()) if x.strip()), "")
            lines += [f"[{i}/{n}] PASS: {step}", *([cap(last, 300)] if last else [])]
        elif rc is None:
            lines += [f"[{i}/{n}] FAIL ({text.removeprefix('error: ').removeprefix('command ')}): {step}"]
        else:
            lines += [f"[{i}/{n}] FAIL (exit {rc}): {step}", cap(text.rstrip("\n"), share)]
    return ToolResult(not failed, "\n".join(lines))


class Shell:
    name = "shell"
    description = (
        "Run a shell command inside the repository (timeboxed). Use for builds, scripts, inspection."
    )
    parameters = {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await run_cmd(str(args["cmd"]), ctx, shell=True)


class RunTests:
    name = "run_tests"
    description = (
        "Run the project's test commands and return each one's result and output. Call this after every "
        "change. In a story layer it runs your own tests: the ones you added or changed."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.test_steps and ctx.scope_base:
            return await _run_scoped(ctx)
        if ctx.test_steps:
            return await run_steps(ctx.test_steps, ctx)
        if not ctx.test_cmd:
            return ToolResult(False, "error: no test command configured for this repository")
        return await run_cmd(ctx.test_cmd, ctx, shell=True)


async def _run_scoped(ctx: ToolContext) -> ToolResult:
    """The gate with pytest limited to the layer's own test files, after ruff has formatted and fixed what it
    safely can (the gate's ruff steps then pass or report only what needs thought). That is the whole of it:
    the pipeline runs the whole gate once the subtask is done and reviewed. A pytest step with no test file to
    run is left out, and the run says so (the layer is written test-first: its tests come before the code)."""
    await run_steps(
        autofix_steps(ctx.test_steps), ctx
    )  # formatting and safe lint fixes: the result is not news
    files = changed_test_files(ctx.worktree, ctx.scope_base)
    result = await run_steps(scoped_steps(ctx.test_steps, files), ctx)
    if not files:
        return ToolResult(
            False, result.output + "\nno test file of yours to run yet: write the layer's tests first."
        )
    head = f"scoped to your {len(files)} test file(s); the pipeline runs the whole suite after review\n"
    return ToolResult(result.ok, head + result.output)


class Git:
    name = "git"
    description = (
        "Run a read/write local git command (status, diff, add, commit, log). Network commands are refused."
    )
    parameters = {
        "type": "object",
        "properties": {"args": {"type": "array", "items": {"type": "string"}}},
        "required": ["args"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        argv = [str(a) for a in (args.get("args") or [])]
        if not argv:
            return ToolResult(False, "error: git needs arguments")
        if argv[0].startswith("-"):  # -c k=v, -C dir, --git-dir ...: the subcommand after them goes unchecked
            return ToolResult(False, "error: git's own options are not allowed: start with the subcommand")
        if argv[0] in GIT_DENY:
            return ToolResult(False, f"error: git {argv[0]} is not allowed inside a team worktree")
        return await run_cmd(
            ["git", "-c", "user.name=airlie", "-c", "user.email=airlie@local", *argv], ctx, shell=False
        )


def quote(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)
