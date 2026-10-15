"""Builtin process tools: shell, run_tests, git. All timeboxed, output-capped, run inside the worktree."""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.sandbox import cap

GIT_DENY = {"push", "fetch", "pull", "remote", "clone", "submodule", "gc", "reflog", "worktree", "config"}


async def run_cmd(cmd: list[str] | str, ctx: ToolContext, *, shell: bool) -> ToolResult:
    try:
        if shell:
            proc = await asyncio.create_subprocess_shell(
                str(cmd), cwd=ctx.worktree, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=ctx.worktree, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
    except OSError as e:
        return ToolResult(False, f"error: {e}")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=ctx.shell_timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolResult(False, f"error: command timed out after {ctx.shell_timeout_s:g}s")
    text = out.decode("utf-8", "replace")  # pragma: no mutate  (codec names are case-insensitive)
    rc = proc.returncode or 0
    body = cap(text, ctx.output_cap_chars)
    if rc != 0:
        body += f"\n[exit {rc}]"
    return ToolResult(rc == 0, body)


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
    description = "Run the project's test command and return its output. Call this after every change."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not ctx.test_cmd:
            return ToolResult(False, "error: no test command configured for this repository")
        return await run_cmd(ctx.test_cmd, ctx, shell=True)


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
        if argv[0] in GIT_DENY:
            return ToolResult(False, f"error: git {argv[0]} is not allowed inside a team worktree")
        return await run_cmd(
            ["git", "-c", "user.name=airlie", "-c", "user.email=airlie@local", *argv], ctx, shell=False
        )


def quote(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)
