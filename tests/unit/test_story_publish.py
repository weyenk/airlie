"""Publisher: gh stack commands, in order, in the publish worktree; drafts only; no merge. One test drives the
real gh-stack against a local bare remote (skipped where gh-stack is not installed)."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from airlie.story import publish
from airlie.story.publish import Publisher, PublishError, run_cmd
from airlie.teams.worktree import add_worktree, checkout_worktree, commit_wip, remove_worktree
from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests


class Recorded:
    def __init__(self, view: str | None = None) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.view = view

    async def __call__(self, args: list[str], cwd: Path) -> str:
        self.calls.append((args, cwd))
        if args[:3] == ["gh", "stack", "view"]:
            if self.view is None:
                raise PublishError("not a stack")
            return self.view
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps({"number": 7, "url": "https://github.com/o/r/pull/7"})
        return ""


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


async def test_check_names_what_is_missing(tmp_path: Path) -> None:
    async def failing(args: list[str], cwd: Path) -> str:
        raise PublishError("no")

    problems = await Publisher(run=failing).check(tmp_path)
    assert len(problems) == 3 and "gh auth login" in problems[0] and "gh-stack" in problems[1]


async def test_run_cmd_returns_stdout_and_raises_with_the_output_tail(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    assert (await run_cmd(["git", "--version"], repo)).startswith("git version")
    with pytest.raises(PublishError, match="exited 128: fatal: Needed a single revision"):
        await run_cmd(["git", "rev-parse", "--verify", "nope"], repo)


async def test_run_cmd_runs_the_command_with_prompts_and_update_notices_off(tmp_path: Path) -> None:
    script = 'echo "$GIT_TERMINAL_PROMPT $GH_PROMPT_DISABLED $GH_STACK_NO_UPDATE_NOTIFIER"'
    assert await run_cmd(["sh", "-c", script], tmp_path) == "0 1 1\n"


async def test_run_cmd_kills_and_reaps_a_command_that_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("airlie.story.publish._TIMEOUT_S", 1)
    pidfile = tmp_path / "pid"
    sleeper = f"echo $$ > {shlex.quote(str(pidfile))}; exec sleep 30"  # exec: the shell's pid is sleep's
    with pytest.raises(PublishError, match=r"timed out after 1 s"):
        # killed at the timeout, not left to run its 30 s out: the outer 10 s makes that regression fail fast
        await asyncio.wait_for(run_cmd(["sh", "-c", sleeper], tmp_path), timeout=10)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)  # gone: killed, and reaped (a zombie would still answer)


async def test_a_command_that_cannot_start_is_a_publish_error_so_check_can_report_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: no gh, no git
    with pytest.raises(PublishError, match="could not start"):
        await run_cmd(["gh", "--version"], tmp_path)
    assert len(await Publisher().check(tmp_path)) == 3  # named, not a traceback


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
