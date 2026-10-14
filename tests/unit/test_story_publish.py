"""Publisher: gh stack commands, in order, in the publish worktree; drafts only; no merge. One test drives the
real gh-stack against a local bare remote (skipped where gh-stack is not installed)."""

from __future__ import annotations

import asyncio
import json
import random
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from airlie.story import publish
from airlie.story.publish import CannotStart, Publisher, PublishError, run_cmd
from airlie.teams.worktree import add_worktree, checkout_worktree, commit_wip, remove_worktree
from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests


class Recorded:
    def __init__(self, view: str | None = None, init_refusal: str | None = None) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.view = view
        self.init_refusal = init_refusal  # what gh-stack says when it will not init

    async def __call__(self, args: list[str], cwd: Path) -> str:
        self.calls.append((args, cwd))
        if args[:3] == ["gh", "stack", "view"]:
            if self.view is None:
                raise PublishError("not a stack")
            return self.view
        if args[:3] == ["gh", "stack", "init"] and self.init_refusal:
            raise PublishError(f"`{' '.join(args)}` exited 5: {self.init_refusal}")
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps({"number": 7, "url": "https://github.com/o/r/pull/7"})
        return ""


def _running(pattern: str) -> bool:
    """Whether some process has `pattern` in its command line."""
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


async def _eventually(check: Callable[[], bool], within: float = 5.0) -> bool:
    """`check` turned true within `within` seconds (a killed process is reaped a moment later)."""
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def _sleeper() -> str:
    """A `sleep 30` that no other process on the machine has the command line of."""
    return f"sleep 30.{random.randrange(10**8, 10**9)}"


async def test_stack_submit_describe_run_in_the_publish_worktree_and_open_drafts(tmp_path: Path) -> None:
    rec = Recorded()
    p = Publisher(remote="upstream", run=rec)
    wt, body = tmp_path / "publish", tmp_path / "pr-01.md"
    await p.stack(wt, "main", ["s-01-a", "s-02-b"])
    await p.submit(wt)
    assert await p.pr(wt, "s-01-a") == (7, "https://github.com/o/r/pull/7")
    await p.describe(wt, "s-01-a", "[1/2] Story: A", body)
    cmds = [c for c, _ in rec.calls]
    assert cmds == [
        ["gh", "stack", "view", "--json"],
        ["gh", "stack", "init", "--base", "main", "s-01-a", "s-02-b"],
        ["gh", "stack", "submit", "--auto", "--remote", "upstream"],
        ["gh", "pr", "view", "s-01-a", "--json", "number,url"],
        ["gh", "pr", "edit", "s-01-a", "--title", "[1/2] Story: A", "--body-file", str(body)],
    ]
    assert all(cwd == wt for _, cwd in rec.calls)
    assert not any("--open" in c or "merge" in " ".join(c) for c in cmds)


async def test_a_stack_that_already_holds_the_layers_is_left_alone(tmp_path: Path) -> None:
    rec = Recorded(view=json.dumps({"trunk": "main", "branches": [{"name": "s-01-a"}, {"name": "s-02-b"}]}))
    await Publisher(run=rec).stack(tmp_path, "main", ["s-01-a", "s-02-b"])
    assert [c[:3] for c, _ in rec.calls] == [["gh", "stack", "view"]]


@pytest.mark.parametrize(
    ("stacked_on", "stacked", "publishing"),
    [
        ("develop", ["s-01-a", "s-02-b"], ["s-01-a", "s-02-b"]),  # the same layers, on another trunk
        ("main", ["s-01-a", "s-02-b"], ["s-01-a", "s-02-b", "s-03-c"]),  # only some of the layers
    ],
    ids=["same-layers-on-another-trunk", "only-some-of-the-layers"],
)
async def test_a_stack_that_is_not_these_layers_on_this_trunk_is_not_left_alone(
    tmp_path: Path, stacked_on: str, stacked: list[str], publishing: list[str]
) -> None:
    view = json.dumps({"trunk": stacked_on, "branches": [{"name": b} for b in stacked]})
    rec = Recorded(view=view, init_refusal='current branch "s-02-b" is already part of a stack')
    match = r"`gh stack init --base main s-01-a .+` exited 5: .*already part of a stack"
    with pytest.raises(PublishError, match=match):  # init was tried, and gh-stack's refusal got through
        await Publisher(run=rec).stack(tmp_path, "main", publishing)
    assert [c[:3] for c, _ in rec.calls] == [["gh", "stack", "view"], ["gh", "stack", "init"]]


async def test_a_branch_name_gh_would_read_as_an_option_is_refused_before_any_command_runs(
    tmp_path: Path,
) -> None:
    rec = Recorded()
    p = Publisher(run=rec)
    with pytest.raises(PublishError, match="'--add-reviewer'"):
        await p.stack(tmp_path, "main", ["s-01-a", "--add-reviewer"])
    with pytest.raises(PublishError, match="'--web'"):
        await p.pr(tmp_path, "--web")
    with pytest.raises(PublishError, match="'-R'"):
        await p.describe(tmp_path, "-R", "title", tmp_path / "body.md")
    assert rec.calls == []


async def test_check_names_what_is_missing(tmp_path: Path) -> None:
    async def failing(args: list[str], cwd: Path) -> str:
        raise PublishError("no")

    problems = await Publisher(run=failing).check(tmp_path)
    assert len(problems) == 3 and "gh auth login" in problems[0] and "gh-stack" in problems[1]


async def test_check_says_gh_is_not_installed_and_skips_what_depends_on_it(tmp_path: Path) -> None:
    asked: list[list[str]] = []

    async def no_gh(args: list[str], cwd: Path) -> str:
        asked.append(args)
        if args[0] == "gh":
            raise CannotStart("`gh auth status` could not start")
        return ""

    assert await Publisher(run=no_gh).check(tmp_path) == ["gh is not installed (https://cli.github.com)"]
    assert [a[:2] for a in asked] == [["gh", "auth"], ["git", "remote"]]  # not gh stack: no gh to ask


async def test_run_cmd_returns_stdout_and_raises_with_the_output_tail(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    assert (await run_cmd(["git", "--version"], repo)).startswith("git version")
    with pytest.raises(PublishError, match="exited 128: fatal: Needed a single revision"):
        await run_cmd(["git", "rev-parse", "--verify", "nope"], repo)


async def test_a_failure_message_keeps_the_first_200_and_the_last_600_characters(tmp_path: Path) -> None:
    async def message_after(output: str) -> str:
        script = f"import sys; sys.stderr.write({output!r}); sys.exit(3)"
        with pytest.raises(PublishError) as e:
            await run_cmd([sys.executable, "-c", script], tmp_path)
        return str(e.value).split(" exited 3: ", 1)[1]

    assert await message_after("a" * 200 + "b" * 600) == "a" * 200 + "b" * 600  # 800 characters: all of it
    assert await message_after("a" * 201 + "b" * 600) == "a" * 200 + "…" + "b" * 600  # 801: the 201st goes
    assert await message_after("H" * 200 + "M" * 5000 + "T" * 600) == "H" * 200 + "…" + "T" * 600
    with pytest.raises(PublishError, match=r"exited 1: only stdout$"):  # no stderr: the stdout
        await run_cmd(["sh", "-c", "echo only stdout; exit 1"], tmp_path)


async def test_a_failure_message_masks_the_gh_token_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = {
        "GH_TOKEN": "ghp_" + "a1B2" * 9,
        "GITHUB_TOKEN": "ghs_" + "c3D4" * 9,
        "GH_ENTERPRISE_TOKEN": "ghe_" + "e5F6" * 9,
        "GITHUB_ENTERPRISE_TOKEN": "ghe_" + "g7H8" * 9,
    }
    for name, value in tokens.items():
        monkeypatch.setenv(name, value)
    names = " ".join(f"${name}" for name in tokens)
    with pytest.raises(PublishError) as e:  # the child sees the tokens and prints them; the message must not
        await run_cmd(["sh", "-c", f'echo "denied: {names}" >&2; exit 1'], tmp_path)
    assert str(e.value).endswith("exited 1: denied: *** *** *** ***")
    assert not any(token in str(e.value) for token in tokens.values())


async def test_a_token_that_holds_another_is_masked_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_abc")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abcdef")  # masking GH_TOKEN first would leave ***def
    with pytest.raises(PublishError, match=r"exited 1: \*\*\*$"):
        await run_cmd(["sh", "-c", "echo $GITHUB_TOKEN >&2; exit 1"], tmp_path)


async def test_an_empty_token_variable_masks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "")
    with pytest.raises(PublishError, match=r"exited 1: plain failure$"):
        await run_cmd(["sh", "-c", "echo plain failure >&2; exit 1"], tmp_path)


async def test_a_token_the_length_limit_would_cut_through_is_masked_before_the_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "ghp_" + "Zq9X" * 9
    monkeypatch.setenv("GH_TOKEN", token)  # printed at character 190, so character 200 falls inside it
    script = "import os, sys; sys.stderr.write('x' * 190 + os.environ['GH_TOKEN'] + 'y' * 1000); sys.exit(1)"
    with pytest.raises(PublishError) as e:
        await run_cmd([sys.executable, "-c", script], tmp_path)
    assert "***" in str(e.value)
    assert token[:8] not in str(e.value) and token[-8:] not in str(e.value)  # not a piece of it either


async def test_run_cmd_runs_the_command_with_prompts_and_update_notices_off(tmp_path: Path) -> None:
    script = 'echo "$GIT_TERMINAL_PROMPT $GH_PROMPT_DISABLED $GH_STACK_NO_UPDATE_NOTIFIER"'
    assert await run_cmd(["sh", "-c", script], tmp_path) == "0 1 1\n"


async def test_run_cmd_kills_and_reaps_a_command_that_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("airlie.story.publish._TIMEOUT_S", 1)
    started: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def recording(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        started.append(await spawn(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    with pytest.raises(PublishError, match=r"timed out after 1 s"):
        # killed at the timeout, not left to run its 30 s out: the outer 10 s makes that regression fail fast
        await asyncio.wait_for(run_cmd(["sleep", "30"], tmp_path), timeout=10)
    # SIGKILLed, and waited for: a process run_cmd had not awaited would not have its exit status yet
    assert started[0].returncode == -signal.SIGKILL


async def test_run_cmd_kills_what_the_command_started_when_it_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh runs the gh-stack extension and that runs git, all holding gh's pipes: killing gh alone leaves them
    running, and waiting for gh then waits for them."""
    monkeypatch.setattr("airlie.story.publish._TIMEOUT_S", 1)
    sleeper = _sleeper()
    started = time.monotonic()
    with pytest.raises(PublishError, match=r"timed out after 1 s"):
        await asyncio.wait_for(run_cmd(["sh", "-c", f"{sleeper} & wait"], tmp_path), timeout=10)
    assert time.monotonic() - started < 5  # it did not sit out the sleep
    assert await _eventually(lambda: not _running(sleeper))  # and the sleep is not left running


async def test_run_cmd_kills_what_the_command_started_when_its_caller_is_cancelled(tmp_path: Path) -> None:
    sleeper = _sleeper()
    task = asyncio.create_task(run_cmd(["sh", "-c", f"{sleeper} & wait"], tmp_path))
    assert await _eventually(lambda: _running(f"^{sleeper}"))  # the sleep itself is up, not only its shell
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)  # at once, not when the sleep has run its 30 s out
    assert await _eventually(lambda: not _running(sleeper))


async def test_a_command_that_cannot_start_is_a_publish_error_so_check_can_report_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: no gh, no git
    with pytest.raises(CannotStart, match="could not start"):
        await run_cmd(["gh", "--version"], tmp_path)
    assert await Publisher().check(tmp_path) == [  # named, not a traceback
        "gh is not installed (https://cli.github.com)",
        f"{tmp_path} has no remote 'origin' (story.remote)",
    ]


def test_nothing_in_the_publisher_merges() -> None:
    names = [*dir(publish), *dir(Publisher)]
    assert not [n for n in names if "merge" in n.lower()]  # only a human merges


def _gh_stack() -> bool:
    if shutil.which("gh") is None:
        return False
    return subprocess.run(["gh", "stack", "--help"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _gh_stack(), reason="needs gh with the gh-stack extension")
async def test_real_gh_stack_adopts_the_layers_without_moving_the_persons_checkout(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", trunk)
    (repo / "README.md").write_text("uncommitted\n")  # the person's work in progress
    for name, base in (("s-01-a", trunk), ("s-02-b", "s-01-a")):
        wt = add_worktree(repo, tmp_path / name, branch=name, start_point=base)
        (wt / f"{name}.txt").write_text(name)
        commit_wip(wt, name)
        remove_worktree(repo, wt)
    pub = checkout_worktree(repo, tmp_path / "publish", "s-02-b")
    p = Publisher()
    await p.stack(pub, trunk, ["s-01-a", "s-02-b"])
    await p.stack(pub, trunk, ["s-01-a", "s-02-b"])  # already a stack: left alone
    await run_cmd(["gh", "stack", "push"], pub)
    assert {"s-01-a", "s-02-b"} <= set(git(remote, "branch", "--format=%(refname:short)").split())
    assert git(repo, "branch", "--show-current") == trunk
    assert git(repo, "status", "--porcelain") == "M README.md"
