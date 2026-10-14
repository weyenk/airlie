"""Publishing a story's layers with gh stack: adopt the layer branches as a stack, push them and open draft
PRs, describe each PR. It runs in a worktree of its own: `gh stack init` switches the current checkout to the
top branch (gh-stack v0.2.1, seen 2026-10-14). There is no merge here, by design: only a human merges."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

Runner = Callable[[list[str], Path], Awaitable[str]]
_TIMEOUT_S = 600


class PublishError(RuntimeError):
    """A git or gh command failed or could not start; the message carries the end of its output."""


async def run_cmd(args: list[str], cwd: Path) -> str:
    """Run one command without a terminal (gh and git never prompt) and return its stdout."""
    quiet = {"GH_STACK_NO_UPDATE_NOTIFIER": "1", "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"}
    env = {**os.environ, **quiet}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:  # gh or git is not installed (`check` reports that), or cwd is gone
        raise PublishError(f"`{' '.join(args)}` could not start: {e}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()  # reap it: no zombie
        raise PublishError(f"`{' '.join(args)}` timed out after {_TIMEOUT_S} s") from None
    if proc.returncode != 0:
        tail = (err or out).decode(errors="replace").strip()[-800:]
        raise PublishError(f"`{' '.join(args)}` exited {proc.returncode}: {tail}")
    return out.decode(errors="replace")


class Publisher:
    def __init__(self, *, remote: str = "origin", run: Runner = run_cmd) -> None:
        self.remote = remote
        self.run = run

    async def _stacked(self, wt: Path) -> list[str] | None:
        try:
            view = json.loads(await self.run(["gh", "stack", "view", "--json"], wt))
            return [str(b["name"]) for b in view["branches"]]
        except (PublishError, ValueError, KeyError, TypeError):
            return None  # not a stack yet

    async def stack(self, wt: Path, trunk: str, branches: list[str]) -> None:
        """Adopt `branches` (bottom first) as a stack on `trunk`; a stack that already holds exactly them (an
        interrupted publish) is left alone."""
        if await self._stacked(wt) == branches:
            return
        await self.run(["gh", "stack", "init", "--base", trunk, *branches], wt)

    async def submit(self, wt: Path) -> None:
        """Push every layer and open the missing PRs as drafts (`--auto`: no editor; never `--open`)."""
        await self.run(["gh", "stack", "submit", "--auto", "--remote", self.remote], wt)

    async def pr(self, wt: Path, branch: str) -> tuple[int, str]:
        j = json.loads(await self.run(["gh", "pr", "view", branch, "--json", "number,url"], wt))
        return int(j["number"]), str(j["url"])

    async def describe(self, wt: Path, branch: str, title: str, body: Path) -> None:
        await self.run(["gh", "pr", "edit", branch, "--title", title, "--body-file", str(body)], wt)

    async def check(self, repo: Path) -> list[str]:
        """What publishing needs, for `task story:check`: gh logged in, gh-stack installed, the remote."""
        no_remote = f"{repo} has no remote '{self.remote}' (story.remote)"
        needs = (
            (["gh", "auth", "status"], "gh is not logged in: run `gh auth login`"),
            (["gh", "stack", "--help"], "gh-stack is missing: `gh extension install github/gh-stack`"),
            (["git", "remote", "get-url", self.remote], no_remote),
        )
        problems: list[str] = []
        for args, why in needs:
            try:
                await self.run(list(args), repo)
            except PublishError:
                problems.append(why)
        return problems
