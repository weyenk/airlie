"""Publishing a story's layers with gh stack: adopt the layer branches as a stack, push them and open draft
PRs, describe each PR. It runs in a worktree of its own: `gh stack init` switches the current checkout to the
top branch (gh-stack v0.2.1, seen 2026-10-14). There is no merge here, by design: only a human merges."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

Runner = Callable[[list[str], Path], Awaitable[str]]
_TIMEOUT_S = 600
_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
_HEAD, _TAIL = 200, 600  # how much of a failed command's output a PublishError keeps


class PublishError(RuntimeError):
    """A git or gh command failed or could not start; the message carries the ends of its output, with the
    values of gh's token variables masked (a stop reason is posted to Jira)."""


class CannotStart(PublishError):
    """The program could not be started: not installed, not executable, or its directory is gone."""


def _mask(text: str, env: Mapping[str, str]) -> str:
    """`text` with the values of gh's token variables replaced by ***. The longest goes first, so a token that
    contains another is masked whole."""
    for token in sorted({env[name] for name in _TOKENS if env.get(name)}, key=len, reverse=True):
        text = text.replace(token, "***")
    return text


def _ends(text: str) -> str:
    """The first 200 and the last 600 characters of `text`, joined by … when it is longer: gh says what
    it was doing first and why it failed last."""
    return text if len(text) <= _HEAD + _TAIL else f"{text[:_HEAD]}…{text[-_TAIL:]}"


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the command and everything it started, then reap it. `wait` returns only once every holder of
    the command's pipes is gone, so a child left running would hold it up (and go on pushing)."""
    with contextlib.suppress(ProcessLookupError):  # nothing of it is left
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


async def run_cmd(args: list[str], cwd: Path) -> str:
    """Run one command without a terminal (gh and git never prompt) and return its stdout. It runs in a
    process group of its own (gh starts the gh-stack extension, which starts git): a timeout, or the caller
    being cancelled, kills all of them."""
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
            start_new_session=True,  # its own process group: killpg reaches what it starts
        )
    except OSError as e:  # gh or git is not installed (`check` reports that), or cwd is gone
        raise CannotStart(f"`{' '.join(args)}` could not start: {e}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except TimeoutError:
        await _kill_group(proc)
        raise PublishError(f"`{' '.join(args)}` timed out after {_TIMEOUT_S} s") from None
    except BaseException:  # cancelled: nothing of the command may outlive its caller
        await _kill_group(proc)
        raise
    if proc.returncode != 0:
        # masked before it is cut: a cut through a token would leave a piece of it that no longer matches
        tail = _ends(_mask((err or out).decode(errors="replace").strip(), env))
        raise PublishError(f"`{' '.join(args)}` exited {proc.returncode}: {tail}")
    return out.decode(errors="replace")


def _refuse_option_like(*names: str) -> None:
    """A branch name is a positional argument to gh: one that starts with "-" would be read as an option
    (--web, --add-reviewer, ...), so it is refused before any command runs."""
    for name in names:
        if name.startswith("-"):
            raise PublishError(f"refusing the branch name {name!r}: gh would read it as an option")


class Publisher:
    def __init__(self, *, remote: str = "origin", run: Runner = run_cmd) -> None:
        self.remote = remote
        self.run = run

    async def _stacked(self, wt: Path) -> tuple[str, list[str]] | None:
        """The trunk and the branches (bottom first) of the stack `wt` is on; None when it is on none."""
        try:
            view = json.loads(await self.run(["gh", "stack", "view", "--json"], wt))
            return str(view["trunk"]), [str(b["name"]) for b in view["branches"]]
        except (PublishError, ValueError, KeyError, TypeError):
            return None  # not a stack yet

    async def stack(self, wt: Path, trunk: str, branches: list[str]) -> None:
        """Adopt `branches` (bottom first) as a stack on `trunk`; a stack that already holds exactly them on
        that trunk (an interrupted publish) is left alone. Any other stack is not: init runs, and gh-stack's
        refusal is the error."""
        _refuse_option_like(*branches)
        if await self._stacked(wt) == (trunk, branches):
            return
        await self.run(["gh", "stack", "init", "--base", trunk, *branches], wt)

    async def submit(self, wt: Path) -> None:
        """Push every layer and open the missing PRs as drafts (`--auto`: no editor; never `--open`)."""
        await self.run(["gh", "stack", "submit", "--auto", "--remote", self.remote], wt)

    async def pr(self, wt: Path, branch: str) -> tuple[int, str]:
        _refuse_option_like(branch)
        j = json.loads(await self.run(["gh", "pr", "view", branch, "--json", "number,url"], wt))
        return int(j["number"]), str(j["url"])

    async def describe(self, wt: Path, branch: str, title: str, body: Path) -> None:
        _refuse_option_like(branch)
        await self.run(["gh", "pr", "edit", branch, "--title", title, "--body-file", str(body)], wt)

    async def check(self, repo: Path) -> list[str]:
        """What publishing needs, for `task story:check`: gh installed and logged in, gh-stack installed, the
        remote. Without gh there is nothing to say about its login or its extension."""
        no_remote = f"{repo} has no remote '{self.remote}' (story.remote)"
        needs = (
            (["gh", "auth", "status"], "gh is not logged in: run `gh auth login`"),
            (["gh", "stack", "--help"], "gh-stack is missing: `gh extension install github/gh-stack`"),
            (["git", "remote", "get-url", self.remote], no_remote),
        )
        problems: list[str] = []
        no_gh = False
        for args, why in needs:
            if no_gh and args[0] == "gh":
                continue  # without gh there is no login and no extension to ask about
            try:
                await self.run(list(args), repo)
            except PublishError as e:
                if args[0] == "gh" and isinstance(e, CannotStart):
                    no_gh = True
                    problems.append("gh is not installed (https://cli.github.com)")
                else:
                    problems.append(why)
        return problems
