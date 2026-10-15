"""Builtin process tools: shell, run_tests, git. All timeboxed, output-capped, run inside the worktree with
the agent environment (airlie.subproc: no credentials)."""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from airlie.subproc import agent_env, kill_group
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.sandbox import cap

GIT_DENY = {"push", "fetch", "pull", "remote", "clone", "submodule", "gc", "reflog", "worktree", "config"}


async def run_cmd(cmd: list[str] | str, ctx: ToolContext, *, shell: bool) -> ToolResult:
    """Run a command a model chose, or code a model wrote, in the worktree: with the agent environment, no
    stdin, and in a process group of its own, so a timeout or the caller being cancelled kills the command
    and everything it started."""
    spawn: dict[str, Any] = dict(
        cwd=ctx.worktree,
        env=agent_env(),
        stdin=asyncio.subprocess.DEVNULL,  # never the person's terminal
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # its own process group: kill_group reaches what it starts
    )
    try:
        if shell:
            proc = await asyncio.create_subprocess_shell(str(cmd), **spawn)
        else:
            proc = await asyncio.create_subprocess_exec(*cmd, **spawn)
    except OSError as e:
        return ToolResult(False, f"error: {e}")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=ctx.shell_timeout_s)
    except TimeoutError:
        await kill_group(proc)
        return ToolResult(False, f"error: command timed out after {ctx.shell_timeout_s:g}s")
    except BaseException:  # cancelled: nothing the command started may outlive its caller
        await kill_group(proc)
        raise
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
