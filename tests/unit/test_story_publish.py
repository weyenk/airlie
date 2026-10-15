"""Publisher: gh stack commands, in order, in the publish worktree; drafts only; no merge. Every step runs as
you (person_env) or as a GitHub App's bot (bot_env, a token minted for the step), and neither lets the
repository's own git configuration choose a credential helper or an ssh command. One test drives the real
gh-stack against a local bare remote (skipped where gh-stack is not installed)."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from airlie.story import publish
from airlie.story.publish import (
    BOT_GIT,
    PINNED,
    CannotStart,
    PersonGit,
    Publisher,
    PublishError,
    bot_env,
    person_env,
    run_cmd,
)
from airlie.subproc import NO_HOOKS, url_credential_helpers, with_git_config
from airlie.teams.worktree import add_worktree, checkout_worktree, commit_wip, remove_worktree
from tests.unit.git_helpers import git
from tests.unit.github_helpers import BOT, FakeGitHub, fake_app, fake_gh
from tests.unit.proc_helpers import eventually, running, unique_sleep
from tests.unit.test_fork_orchestrator import repo_with_marker_tests


class Recorded:
    def __init__(self, view: str | None = None, init_refusal: str | None = None) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.envs: list[dict[str, str]] = []  # each call's environment
        self.view = view
        self.init_refusal = init_refusal  # what gh-stack says when it will not init

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        self.calls.append((args, cwd))
        self.envs.append(dict(env))
        if args[:3] == ["gh", "stack", "view"]:
            if self.view is None:
                raise PublishError("not a stack")
            return self.view
        if args[:3] == ["gh", "stack", "init"] and self.init_refusal:
            raise PublishError(f"`{' '.join(args)}` exited 5: {self.init_refusal}")
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
    async def failing(args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        raise PublishError("no")

    problems, notes = await Publisher(run=failing).check(tmp_path)
    assert len(problems) == 3 and "gh auth login" in problems[0] and "gh-stack" in problems[1]
    assert notes == []


async def test_check_says_gh_is_not_installed_and_skips_what_depends_on_it(tmp_path: Path) -> None:
    asked: list[list[str]] = []

    async def no_gh(args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        asked.append(args)
        if args[0] == "gh":
            raise CannotStart("`gh auth status` could not start")
        return ""

    problems, _ = await Publisher(run=no_gh).check(tmp_path)
    assert problems == ["gh is not installed (https://cli.github.com)"]
    assert [a[:2] for a in asked] == [["gh", "auth"], ["git", "remote"]]  # not gh stack: no gh to ask


def yours() -> dict[str, str]:
    """A publish step's environment as you, with no git settings of yours to pin."""
    return person_env(PersonGit())


async def test_run_cmd_returns_stdout_and_raises_with_the_output_tail(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    assert (await run_cmd(["git", "--version"], repo, yours())).startswith("git version")
    with pytest.raises(PublishError, match="exited 128: fatal: Needed a single revision"):
        await run_cmd(["git", "rev-parse", "--verify", "nope"], repo, yours())


async def test_a_failure_message_keeps_the_first_200_and_the_last_600_characters(tmp_path: Path) -> None:
    async def message_after(output: str) -> str:
        script = f"import sys; sys.stderr.write({output!r}); sys.exit(3)"
        with pytest.raises(PublishError) as e:
            await run_cmd([sys.executable, "-c", script], tmp_path, yours())
        return str(e.value).split(" exited 3: ", 1)[1]

    assert await message_after("a" * 200 + "b" * 600) == "a" * 200 + "b" * 600  # 800 characters: all of it
    assert await message_after("a" * 201 + "b" * 600) == "a" * 200 + "…" + "b" * 600  # 801: the 201st goes
    assert await message_after("H" * 200 + "M" * 5000 + "T" * 600) == "H" * 200 + "…" + "T" * 600
    with pytest.raises(PublishError, match=r"exited 1: only stdout$"):  # no stderr: the stdout
        await run_cmd(["sh", "-c", "echo only stdout; exit 1"], tmp_path, yours())


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
        await run_cmd(["sh", "-c", f'echo "denied: {names}" >&2; exit 1'], tmp_path, yours())
    assert str(e.value).endswith("exited 1: denied: *** *** *** ***")
    assert not any(token in str(e.value) for token in tokens.values())


async def test_a_token_that_holds_another_is_masked_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_abc")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abcdef")  # masking GH_TOKEN first would leave ***def
    with pytest.raises(PublishError, match=r"exited 1: \*\*\*$"):
        await run_cmd(["sh", "-c", "echo $GITHUB_TOKEN >&2; exit 1"], tmp_path, yours())


async def test_an_empty_token_variable_masks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "")
    with pytest.raises(PublishError, match=r"exited 1: plain failure$"):
        await run_cmd(["sh", "-c", "echo plain failure >&2; exit 1"], tmp_path, yours())


async def test_a_token_the_length_limit_would_cut_through_is_masked_before_the_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "ghp_" + "Zq9X" * 9
    monkeypatch.setenv("GH_TOKEN", token)  # printed at character 190, so character 200 falls inside it
    script = "import os, sys; sys.stderr.write('x' * 190 + os.environ['GH_TOKEN'] + 'y' * 1000); sys.exit(1)"
    with pytest.raises(PublishError) as e:
        await run_cmd([sys.executable, "-c", script], tmp_path, yours())
    assert "***" in str(e.value)
    assert token[:8] not in str(e.value) and token[-8:] not in str(e.value)  # not a piece of it either


@pytest.mark.parametrize("as_who", ["you", "the bot"])
async def test_a_publish_step_runs_with_prompts_askpass_and_update_notices_off(
    tmp_path: Path, as_who: str
) -> None:
    env = yours() if as_who == "you" else bot_env("ghs_t", tmp_path)
    script = 'echo "$GIT_TERMINAL_PROMPT $GIT_ASKPASS $SSH_ASKPASS $GH_PROMPT_DISABLED"'
    script += '; echo "$GH_STACK_NO_UPDATE_NOTIFIER"'
    assert await run_cmd(["sh", "-c", script], tmp_path, env) == "0 false false 1\n1\n"


@pytest.mark.parametrize("hooks", ["hooks directory", "relative core.hooksPath"])
async def test_a_push_runs_no_pre_push_hook_a_model_planted(
    tmp_path: Path, local_remotes: None, hooks: str
) -> None:
    """Publishing keeps the person's credentials, so it must run nothing a model's shell left in the
    repository; the push itself still happens."""
    repo = repo_with_marker_tests(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    hook = repo / ".git" / "hooks" / "pre-push"
    if hooks == "relative core.hooksPath":
        git(repo, "config", "core.hooksPath", ".hooks")
        hook = repo / ".hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    mark = tmp_path / "pre-push ran"
    hook.write_text(f"#!/bin/sh\ntouch '{mark}'\n")
    hook.chmod(0o755)
    await asyncio.wait_for(
        run_cmd(["git", "push", "-q", "origin", "HEAD:refs/heads/layer"], repo, yours()), 60
    )
    assert not mark.exists()
    assert git(remote, "rev-parse", "layer") == git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/plain")  # the control: plain git runs the hook
    assert mark.exists()


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
        await asyncio.wait_for(run_cmd(["sleep", "30"], tmp_path, yours()), timeout=10)
    # SIGKILLed, and waited for: a process run_cmd had not awaited would not have its exit status yet
    assert started[0].returncode == -signal.SIGKILL


async def test_run_cmd_kills_what_the_command_started_when_it_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh runs the gh-stack extension and that runs git, all holding gh's pipes: killing gh alone leaves them
    running, and waiting for gh then waits for them."""
    monkeypatch.setattr("airlie.story.publish._TIMEOUT_S", 1)
    sleeper = unique_sleep()
    started = time.monotonic()
    with pytest.raises(PublishError, match=r"timed out after 1 s"):
        await asyncio.wait_for(run_cmd(["sh", "-c", f"{sleeper} & wait"], tmp_path, yours()), timeout=10)
    assert time.monotonic() - started < 5  # it did not sit out the sleep
    assert await eventually(lambda: not running(sleeper))  # and the sleep is not left running


async def test_run_cmd_kills_what_the_command_started_when_its_caller_is_cancelled(tmp_path: Path) -> None:
    sleeper = unique_sleep()
    task = asyncio.create_task(run_cmd(["sh", "-c", f"{sleeper} & wait"], tmp_path, yours()))
    assert await eventually(lambda: running(f"^{sleeper}"))  # the sleep itself is up, not only its shell
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)  # at once, not when the sleep has run its 30 s out
    assert await eventually(lambda: not running(sleeper))


async def test_a_command_that_cannot_start_is_a_publish_error_so_check_can_report_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: no gh, no git
    with pytest.raises(CannotStart, match="could not start"):
        await run_cmd(["gh", "--version"], tmp_path, yours())
    assert await Publisher().check(tmp_path) == (
        [  # named, not a traceback
            "gh is not installed (https://cli.github.com)",
            f"{tmp_path} has no remote 'origin' (story.remote)",
        ],
        [],
    )


def test_nothing_in_the_publisher_merges() -> None:
    names = [*dir(publish), *dir(Publisher)]
    assert not [n for n in names if "merge" in n.lower()]  # only a human merges


def _gh_stack() -> bool:
    if shutil.which("gh") is None:
        return False
    return subprocess.run(["gh", "stack", "--help"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _gh_stack(), reason="needs gh with the gh-stack extension")
async def test_real_gh_stack_adopts_the_layers_without_moving_the_persons_checkout(
    tmp_path: Path, local_remotes: None
) -> None:
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
    await run_cmd(["gh", "stack", "push"], pub, yours())
    assert {"s-01-a", "s-02-b"} <= set(git(remote, "branch", "--format=%(refname:short)").split())
    assert git(repo, "branch", "--show-current") == trunk
    assert git(repo, "status", "--porcelain") == "M README.md"


# ---- who publishes: you (your own git settings pinned) or the bot (a token per step) ----


def run_script(path: Path, body: str) -> Path:
    """An executable shell script at `path`."""
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def git_in(
    cwd: Path, env: Mapping[str, str], *args: str, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    """git as a publish step runs it: with `env`, no terminal, bounded."""
    return subprocess.run(
        ["git", *args], cwd=cwd, env=dict(env), input=stdin, capture_output=True, text=True, timeout=20
    )


def pairs(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """The git configuration `env` gives on the command line, in order."""
    return [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    ]


@pytest.fixture
def own_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A git configuration of the test's own in place of the person's: no system file, and an empty global
    file (returned). A test that runs git over HTTPS keeps it on this machine with `offline`."""
    config = tmp_path / "global-gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    for name in (
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    ):
        monkeypatch.delenv(name, raising=False)
    url_credential_helpers.cache_clear()
    yield config
    url_credential_helpers.cache_clear()


@pytest.fixture
def marks(tmp_path: Path) -> Path:
    """Where the planted and the person's helpers and ssh commands say they ran: one line each time."""
    log = tmp_path / "marks.log"
    log.touch()
    return log


def helper(path: Path, marks: Path, who: str, password: str) -> Path:
    """A credential helper that logs `<who> <operation>` and answers `get` with `password`."""
    return run_script(
        path,
        f'echo "{who} $1" >> {shlex.quote(str(marks))}\n'
        f'[ "$1" = get ] && printf "username={who}\\\\npassword={password}\\\\n"\nexit 0',
    )


def fill(cwd: Path, env: Mapping[str, str], host: str) -> subprocess.CompletedProcess[str]:
    return git_in(cwd, env, "credential", "fill", stdin=f"protocol=https\nhost={host}\n\n")


def logged(marks: Path) -> list[str]:
    return marks.read_text().splitlines()


async def test_as_you_your_own_helpers_and_ssh_command_hold_over_ones_a_model_planted(
    tmp_path: Path, own_git: Path, marks: Path
) -> None:
    """A model's shell can write the repository's .git/config: a helper there would be handed your password
    (git `store`s it in every helper) and an ssh command there would run with your SSH agent."""
    yours_ = helper(tmp_path / "yours.sh", marks, "you", "your-secret")
    your_ssh = run_script(tmp_path / "your-ssh.sh", f"echo 'your ssh' >> {shlex.quote(str(marks))}\nexit 1")
    own_git.write_text(
        own_git.read_text() + f"[credential]\n\thelper = {yours_}\n[core]\n\tsshCommand = {your_ssh}\n"
    )
    repo = repo_with_marker_tests(tmp_path)
    git(
        repo, "config", "credential.helper", str(helper(tmp_path / "planted.sh", marks, "planted", "planted"))
    )
    planted_ssh = run_script(
        tmp_path / "planted-ssh.sh", f"echo 'planted ssh' >> {shlex.quote(str(marks))}\nexit 1"
    )
    git(repo, "config", "core.sshCommand", str(planted_ssh))
    git(repo, "remote", "add", "origin", "ssh://git@example.invalid/o/r.git")
    env = person_env(PersonGit.read(repo))
    out = fill(repo, env, "example.invalid")
    assert out.returncode == 0 and "password=your-secret" in out.stdout
    git_in(repo, env, "credential", "approve", stdin=out.stdout + "\n")
    assert git_in(repo, env, "ls-remote", "origin").returncode != 0  # your ssh ran: a stand-in that fails
    # (git runs an ssh command it does not know twice: once to ask which ssh it is)
    assert set(logged(marks)) == {"you get", "you store", "your ssh"}  # the planted ones never ran
    # the control: plain git hands the planted helper your password and runs the planted ssh command
    marks.write_text("")
    plain = dict(os.environ)
    git_in(repo, plain, "credential", "approve", stdin=out.stdout + "\n")
    git_in(repo, plain, "ls-remote", "origin")
    assert "planted store" in logged(marks) and "planted ssh" in logged(marks)


async def test_as_you_with_no_ssh_command_of_your_own_ssh_is_pinned(tmp_path: Path, own_git: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "config", "core.sshCommand", "./.planted-ssh")
    env = person_env(PersonGit.read(repo))
    assert git_in(repo, env, "config", "core.sshCommand").stdout == "ssh\n"
    assert git_in(repo, env, "config", "--get-all", "credential.helper").stdout == "\n"  # just the reset


def test_your_git_settings_come_from_your_system_and_global_files_in_the_order_git_reads_them(
    tmp_path: Path, own_git: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    included = tmp_path / "included-gitconfig"
    included.write_text('[credential "https://example.invalid"]\n\thelper = from-include\n')
    system = tmp_path / "system-gitconfig"
    system.write_text("[credential]\n\thelper = osxkeychain\n[core]\n\tsshCommand = ssh -F /etc/sys\n")
    own_git.write_text(
        own_git.read_text()
        + '[credential "https://github.com"]\n\thelper =\n\thelper = !gh auth git-credential\n'
        + f"[include]\n\tpath = {included}\n[core]\n\tsshCommand = ssh -i ~/.ssh/work\n"
    )
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "config", "credential.helper", "planted")  # the repository's own: never read
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
    from_global = (
        ("credential.https://github.com.helper", ""),
        ("credential.https://github.com.helper", "!gh auth git-credential"),
        ("credential.https://example.invalid.helper", "from-include"),
    )
    assert PersonGit.read(repo) == PersonGit(
        "ssh -i ~/.ssh/work", from_global
    )  # GIT_CONFIG_NOSYSTEM: no system
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
    you = PersonGit.read(repo)
    assert you.ssh_command == "ssh -i ~/.ssh/work"  # global wins over system
    assert you.helpers == (("credential.helper", "osxkeychain"), *from_global)  # system first, as git reads
    own_git.write_text("")
    assert PersonGit.read(repo).ssh_command == "ssh -F /etc/sys"
    system.write_text("")
    assert PersonGit.read(repo) == PersonGit("ssh", ())
    # pinned on the command line after a reset: git asks exactly your helpers, in your order
    env = person_env(you)
    listed = git_in(repo, env, "config", "--get-all", "credential.helper").stdout.splitlines()
    assert listed[-2:] == ["", "osxkeychain"] and "planted" in listed[:-2]  # the reset comes after it


def test_without_git_your_settings_are_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert PersonGit.read(tmp_path) == PersonGit("ssh", ())


URL = "https://github.com/o/r.git"
LOOPBACK = "github.com:443:127.0.0.1"
"""Where a test sends github.com: nothing listens on this machine's port 443, so a request fails at once."""


def repo_on_github(tmp_path: Path, url: str = "git@github.com:o/r.git") -> Path:
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "remote", "add", "origin", url)
    return repo


class Inspecting(Recorded):
    """Recorded, and what each call's gh configuration directory held when the call ran, and its mode."""

    def __init__(self) -> None:
        super().__init__()
        self.gh_dirs: list[tuple[list[str], int]] = []

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        gh_dir = Path(env["GH_CONFIG_DIR"])
        self.gh_dirs.append((sorted(p.name for p in gh_dir.iterdir()), gh_dir.stat().st_mode & 0o777))
        return await super().__call__(args, cwd, env)


async def every_step(p: Publisher, repo: Path, wt: Path) -> None:
    await p.fetch(repo, "main")
    await p.stack(wt, "main", ["s-01-a", "s-02-b"])
    await p.submit(wt)
    await p.pr(wt, "s-01-a")
    await p.describe(wt, "s-01-a", "[1/2] Story: A", wt / "pr-01.md")


async def test_as_the_bot_each_step_has_a_new_token_its_own_gh_dir_and_none_of_your_credentials(
    tmp_path: Path, own_git: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yours_ = {"GH_TOKEN": "ghp_yours", "GITHUB_TOKEN": "ghp_yours2", "SSH_AUTH_SOCK": "/tmp/agent.sock"}
    for name, value in {**yours_, "GH_CONFIG_DIR": str(tmp_path / "your-gh")}.items():
        monkeypatch.setenv(name, value)
    github, rec = FakeGitHub(), Inspecting()
    repo = repo_on_github(tmp_path)
    p = Publisher(run=rec, app=fake_app(tmp_path, github))
    p.prepare(repo)
    await every_step(p, repo, repo)  # the publish worktree is one of the repository's: it has the remote
    cmds = [c[:3] for c, _ in rec.calls]
    assert cmds == [
        ["git", "fetch", "-q"],
        ["gh", "stack", "view"],
        ["gh", "stack", "init"],
        ["gh", "stack", "submit"],
        ["gh", "pr", "view"],
        ["gh", "pr", "edit"],
    ]
    # a token minted at the start of each step (the stack's two commands share one), for o/r alone
    assert [e["GH_TOKEN"] for e in rec.envs] == [github.tokens[i] for i in (0, 1, 1, 2, 3, 4)]
    assert len(github.tokens) == 5 and github.minted_for() == [["r"]] * 5
    for env in rec.envs:
        assert [n for n in ("GITHUB_TOKEN", "SSH_AUTH_SOCK", "GH_ENTERPRISE_TOKEN") if n in env] == []
        assert env["GIT_SSH_COMMAND"] == "false" and env["GIT_ALLOW_PROTOCOL"] == "https"
        assert env["GIT_PROXY_COMMAND"] == ""
        last = [
            *NO_HOOKS,
            *BOT_GIT,
            *PINNED,
            ("remote.origin.proxy", ""),
            (f"http.{URL}.curloptResolve", ""),  # again for the one URL the step fetches from and pushes to
            (f"http.{URL}.sslVerify", "true"),
        ]
        assert pairs(env)[-len(last) :] == last
    gh_dirs = {e["GH_CONFIG_DIR"] for e in rec.envs}
    assert len(gh_dirs) == 5 and str(tmp_path / "your-gh") not in gh_dirs  # one per step, never yours
    assert rec.gh_dirs == [([], 0o700)] * 6  # empty and private while it runs
    assert not any(Path(d).exists() for d in gh_dirs)  # and gone after


async def test_as_you_each_step_keeps_your_login_and_gets_nothing_of_the_bots(
    tmp_path: Path, own_git: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yours_ = {
        "GH_TOKEN": "ghp_yours",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "GH_CONFIG_DIR": str(tmp_path / "gh"),
    }
    for name, value in yours_.items():
        monkeypatch.setenv(name, value)
    rec = Recorded()
    repo = repo_on_github(tmp_path)
    p = Publisher(run=rec)
    p.prepare(repo)
    await every_step(p, repo, tmp_path)
    assert len(rec.envs) == 6
    for env in rec.envs:
        assert {name: env[name] for name in yours_} == yours_
        assert env["GIT_ALLOW_PROTOCOL"] == "ssh:https" and "GIT_SSH_COMMAND" not in env
        assert env["GIT_PROXY_COMMAND"] == ""
        assert pairs(env) == [
            *NO_HOOKS,
            ("core.sshCommand", "ssh"),
            ("credential.helper", ""),
            *PINNED,
            ("remote.origin.proxy", ""),
        ]
    assert p.repository is None  # nothing is minted, and no repository is pinned


async def test_a_step_whose_token_cannot_be_minted_runs_nothing_and_says_why(tmp_path: Path) -> None:
    github, rec = FakeGitHub(installed=("o/elsewhere",)), Recorded()
    repo = repo_on_github(tmp_path)
    p = Publisher(run=rec, app=fake_app(tmp_path, github))
    p.prepare(repo)
    with pytest.raises(PublishError) as e:
        await p.submit(repo)
    assert str(e.value).startswith("the GitHub App airlie-bot-test is not installed on o/r: install it")
    assert e.value.__cause__ is None and e.value.__context__ is None and rec.calls == []


@pytest.mark.parametrize(
    ("remote", "planted", "value", "said"),
    [
        (
            "git@github.com:o/r.git",
            "url.https://github.com/evil/other.insteadOf",
            "git@github.com:o/r",
            "fetch from",
        ),
        ("git@github.com:o/r.git", "remote.origin.pushurl", "git@github.com:evil/other.git", "push to"),
        (
            "git@github.com:o/r.git",
            "remote.origin.pushurl",
            "https://github.com/o/r.git/",
            "push to",
        ),  # same repo
        (
            URL,
            "url.git@github.com:.pushInsteadOf",
            "https://github.com/",
            "push to",
        ),  # a rule many people have
    ],
)
async def test_as_the_bot_a_step_refuses_a_remote_that_does_not_lead_to_its_repository(
    tmp_path: Path, own_git: Path, remote: str, planted: str, value: str, said: str
) -> None:
    """A redirect a model planted (or a rule of yours) is refused before a token is minted or a command runs.
    The token would cover the story's repository alone anyway, but another URL of it could have http.<url>.*
    settings of its own that the step's pins do not reach."""
    repo = repo_on_github(tmp_path, remote)
    git(repo, "config", planted, value)  # before prepare: it reads the remote's URL as written
    github, rec = FakeGitHub(installed=("o/r", "evil/other")), Recorded()
    p = Publisher(run=rec, app=fake_app(tmp_path, github))
    p.prepare(repo)
    assert p.repository == "o/r"  # and no token follows the redirect
    with pytest.raises(
        PublishError, match=f"^the remote 'origin' would {said} [^ ]+, not https://github.com/o/r: "
    ):
        await p.submit(repo)
    assert rec.calls == [] and github.minted_for() == []


async def test_as_the_bot_a_step_refuses_a_remote_rewritten_after_prepare(
    tmp_path: Path, own_git: Path
) -> None:
    repo = repo_on_github(tmp_path)
    github, rec = FakeGitHub(installed=("o/r", "evil/other")), Recorded()
    p = Publisher(run=rec, app=fake_app(tmp_path, github))
    p.prepare(repo)
    git(repo, "remote", "set-url", "origin", "git@github.com:evil/other.git")  # a model, during the build
    with pytest.raises(
        PublishError, match="would fetch from https://github.com/evil/other.git, not https://github"
    ):
        await p.fetch(repo, "main")
    assert rec.calls == [] and github.minted_for() == []


async def test_as_the_bot_a_step_pins_tls_and_addresses_for_each_url_it_uses(
    tmp_path: Path, own_git: Path
) -> None:
    """A URL-specific http.<url>.* setting outranks a general one; the step gives its own for each URL."""
    repo = repo_on_github(tmp_path)
    git(repo, "config", f"http.{URL}.sslVerify", "false")
    git(repo, "config", f"http.{URL}.curloptResolve", "github.com:443:127.0.0.3")
    rec = Recorded()
    p = Publisher(run=rec, app=fake_app(tmp_path, FakeGitHub()))
    p.prepare(repo)
    await p.submit(repo)
    [env] = rec.envs
    verify = git_in(repo, env, "config", "--get-urlmatch", "http.sslverify", URL).stdout
    said = curl_said(repo, offline(env, URL), "ls-remote", "origin")
    assert verify == "true\n" and "127.0.0.3" not in said and "Trying 127.0.0.1:443" in said
    plain_verify = git_in(repo, dict(os.environ), "config", "--get-urlmatch", "http.sslverify", URL).stdout
    assert plain_verify == "false\n"  # the control


def test_prepare_holds_the_remote_to_an_expected_repository(tmp_path: Path) -> None:
    """The seam for a repository named elsewhere (an issue's label): without one, the remote as written."""
    repo = repo_on_github(tmp_path, "git@github.com:O/R.git")
    p = Publisher(run=Recorded(), app=fake_app(tmp_path, FakeGitHub()))
    p.prepare(repo, expected="o/r")  # GitHub's names are any case
    assert p.repository == "O/R"
    with pytest.raises(PublishError) as e:
        p.prepare(repo, expected="o/other")
    assert str(e.value) == "story.remote 'origin' names O/R, not o/other: the story publishes to o/other"
    you = Publisher(run=Recorded())
    you.prepare(repo, expected="o/r")
    with pytest.raises(PublishError, match="names O/R, not o/other"):
        you.prepare(repo, expected="o/other")


async def test_prepare_names_a_remote_the_bot_cannot_publish_to(tmp_path: Path) -> None:
    p = Publisher(run=Recorded(), app=fake_app(tmp_path, FakeGitHub()))
    repo = repo_with_marker_tests(tmp_path)
    with pytest.raises(PublishError, match=r"^.*/repo has no remote 'origin' \(story.remote\)$"):
        p.prepare(repo)
    git(repo, "remote", "add", "origin", "https://me:hunter2@gitlab.example/o/r.git")
    with pytest.raises(PublishError) as e:
        p.prepare(repo)
    assert str(e.value).startswith(
        "story.remote 'origin': 'https://***@gitlab.example/o/r.git' is not a github"
    )
    assert "hunter2" not in str(e.value) and e.value.__context__ is None


def test_the_bots_git_entries_reach_a_child_git(tmp_path: Path, own_git: Path) -> None:
    repo = repo_on_github(tmp_path)
    git(repo, "remote", "add", "other", "ssh://git@github.com/o/r.git")
    env = bot_env("ghs_child", tmp_path / "gh")
    assert git_in(
        repo, env, "config", "--get-all", "url.https://github.com/.insteadof"
    ).stdout.splitlines() == [
        "git@github.com:",
        "ssh://git@github.com/",
    ]
    helpers = git_in(repo, env, "config", "--get-all", "credential.https://github.com.helper").stdout
    assert helpers.splitlines() == ["!gh auth git-credential"]
    assert git_in(repo, env, "config", "--get-all", "credential.helper").stdout.splitlines()[-1] == ""
    # GitHub's SSH URLs are fetched and pushed over HTTPS: an app token cannot use SSH
    assert git_in(repo, env, "ls-remote", "--get-url", "origin").stdout == "https://github.com/o/r.git\n"
    assert git_in(repo, env, "ls-remote", "--get-url", "other").stdout == "https://github.com/o/r.git\n"


@pytest.fixture
def gh_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `gh` first on PATH; returns the file it logs each credential request to."""
    asked = tmp_path / "gh-asked.log"
    asked.touch()
    fake_gh(tmp_path / "fakebin", asked)
    monkeypatch.setenv("PATH", f"{tmp_path / 'fakebin'}{os.pathsep}{os.environ['PATH']}")
    return asked


async def test_as_the_bot_a_helper_a_model_planted_never_answers_and_gh_answers_only_for_github(
    tmp_path: Path, own_git: Path, marks: Path, gh_asked: Path
) -> None:
    repo = repo_on_github(tmp_path)
    git(
        repo, "config", "credential.helper", str(helper(tmp_path / "planted.sh", marks, "planted", "planted"))
    )
    env = bot_env("ghs_step-token", tmp_path / "gh")
    out = fill(repo, env, "github.com")
    assert out.returncode == 0 and "username=x-access-token\npassword=ghs_step-token" in out.stdout
    other = fill(repo, env, "evil.example")  # a remote redirected to another host gets no credential
    assert (
        other.returncode != 0
        and "password=" not in other.stdout
        and "terminal prompts disabled" in other.stderr
    )
    git_in(repo, env, "credential", "approve", stdin=out.stdout + "\n")
    assert logged(marks) == [] and gh_asked.read_text().splitlines() == ["get github.com", "store github.com"]
    # the control: plain git asks the planted helper
    assert "password=planted" in fill(repo, dict(os.environ), "evil.example").stdout


@pytest.mark.parametrize("protocols", ["https only", "any"])
async def test_as_the_bot_an_ssh_command_a_model_planted_never_runs(
    tmp_path: Path, own_git: Path, marks: Path, protocols: str
) -> None:
    """GIT_SSH_COMMAND outranks core.sshCommand, and git is limited to HTTPS besides."""
    repo = repo_on_github(tmp_path, "ssh://git@evil.example/o/r.git")
    planted = run_script(
        tmp_path / "planted-ssh.sh", f"echo 'planted ssh' >> {shlex.quote(str(marks))}\nexit 1"
    )
    git(repo, "config", "core.sshCommand", str(planted))
    env = bot_env("ghs_t", tmp_path / "gh")
    if protocols == "any":
        del env["GIT_ALLOW_PROTOCOL"]  # GIT_SSH_COMMAND=false alone keeps it from running
    assert git_in(repo, env, "ls-remote", "origin").returncode != 0
    assert logged(marks) == []
    git_in(repo, dict(os.environ), "ls-remote", "origin")  # the control: plain git runs it
    assert "planted ssh" in logged(marks)


async def test_as_the_bot_a_command_a_model_named_for_a_local_remote_never_runs(
    tmp_path: Path, own_git: Path, marks: Path
) -> None:
    """remote.<name>.receivepack is a command git runs itself for a remote on this machine, with the step's
    environment and so its token: as the bot, git uses HTTPS and nothing else."""
    repo = repo_on_github(tmp_path)
    receiver = run_script(
        tmp_path / "receive.sh", f"echo 'planted receivepack' >> {shlex.quote(str(marks))}\nexit 1"
    )
    git(repo, "config", "remote.origin.pushurl", str(tmp_path / "elsewhere.git"))
    git(repo, "config", "remote.origin.receivepack", str(receiver))
    out = git_in(repo, bot_env("ghs_t", tmp_path / "gh"), "push", "origin", "HEAD:refs/heads/x")
    assert out.returncode != 0 and "transport 'file' not allowed" in out.stderr and logged(marks) == []
    git_in(repo, dict(os.environ), "push", "origin", "HEAD:refs/heads/x")  # the control: plain git runs it
    assert logged(marks) == ["planted receivepack"]


async def test_a_failure_as_the_bot_is_masked_with_the_steps_token(tmp_path: Path) -> None:
    token = "ghs_" + "Mint3d" * 6
    with pytest.raises(PublishError) as e:
        await run_cmd(
            ["sh", "-c", 'echo "denied for $GH_TOKEN" >&2; exit 1'], tmp_path, bot_env(token, tmp_path)
        )
    assert str(e.value).endswith("exited 1: denied for ***") and token not in str(e.value)


async def test_check_as_the_bot_asks_no_gh_login_and_says_who_publishes(
    tmp_path: Path, own_git: Path
) -> None:
    github, rec = FakeGitHub(), Recorded()
    repo = repo_on_github(tmp_path)
    found = await Publisher(run=rec, app=fake_app(tmp_path, github)).check(repo)
    assert found == ([], ["publishing to o/r as airlie-bot-test[bot]"])
    assert [c[:3] for c, _ in rec.calls] == [["gh", "stack", "--help"], ["git", "remote", "get-url"]]
    assert not any("GH_TOKEN" in env for env in rec.envs)  # none of these needs a credential


async def test_check_as_the_bot_names_a_remote_it_cannot_publish_to_and_what_the_app_lacks(
    tmp_path: Path, own_git: Path
) -> None:
    repo = repo_on_github(tmp_path, "git@gitlab.com:o/r.git")
    found = await Publisher(run=Recorded(), app=fake_app(tmp_path, FakeGitHub())).check(repo)
    assert len(found.problems) == 1 and found.problems[0].startswith(
        "story.remote 'origin': 'git@gitlab.com:o/r.git'"
    )
    (tmp_path / "two").mkdir()
    repo = repo_on_github(tmp_path / "two")
    found = await Publisher(run=Recorded(), app=fake_app(tmp_path, FakeGitHub(installed=()))).check(repo)
    assert found.notes == [] and found.problems[0].startswith(
        "the GitHub App airlie-bot-test is not installed on o/r"
    )


async def test_check_as_the_bot_without_a_remote_asks_github_nothing(tmp_path: Path) -> None:
    async def no_remote(args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        if args[0] == "git":
            raise PublishError("no such remote")
        return ""

    github = FakeGitHub()
    found = await Publisher(run=no_remote, app=fake_app(tmp_path, github)).check(tmp_path)
    assert found == ([f"{tmp_path} has no remote 'origin' (story.remote)"], []) and github.requests == []


async def test_the_bots_identity_comes_from_the_app_and_yours_is_none(tmp_path: Path) -> None:
    repo = repo_on_github(tmp_path)
    assert await Publisher(run=Recorded()).author(repo) is None
    assert await Publisher(run=Recorded(), app=fake_app(tmp_path, FakeGitHub())).author(repo) == BOT
    failing = Publisher(run=Recorded(), app=fake_app(tmp_path, FakeGitHub(installed=())))
    with pytest.raises(PublishError, match="is not installed on o/r"):
        await failing.author(repo)


# ---- what a model can plant in .git/config for a publish step's git (fix round 1, I1) ----


def offline(env: Mapping[str, str], *urls: str) -> dict[str, str]:
    """`env` with github.com resolved to 127.0.0.1 after everything else in it, in general and for each of
    `urls` (a URL-specific entry outranks a general one), so no request leaves this machine; and without
    NO_PROXY (the unit tests set it to *), so git uses a proxy when one is configured."""
    out = {name: value for name, value in env.items() if name.lower() != "no_proxy"}
    return with_git_config(
        out, [("http.curloptResolve", LOOPBACK), *((f"http.{u}.curloptResolve", LOOPBACK) for u in urls)]
    )


def curl_said(cwd: Path, env: Mapping[str, str], *args: str) -> str:
    """What curl reported doing for `git <args>` under `env`: where it tried to connect, and the addresses
    it was given for github.com."""
    done = git_in(cwd, {**env, "GIT_CURL_VERBOSE": "1"}, *args)
    return "\n".join(line for line in done.stderr.splitlines() if "Trying" in line or "to DNS cache" in line)


def both_ways(tmp_path: Path, as_who: str) -> dict[str, str]:
    """A publish step's environment, as you or as the bot (for a remote named origin)."""
    return yours() if as_who == "you" else bot_env("ghs_t", tmp_path / "gh")


@pytest.mark.parametrize("as_who", ["you", "the bot"])
@pytest.mark.parametrize("planted", ["http.proxy", "remote.origin.proxy", f"http.{URL}.proxy"])
async def test_a_publish_step_uses_no_proxy_a_model_planted(
    tmp_path: Path, own_git: Path, as_who: str, planted: str
) -> None:
    """A proxy sees what git sends through it; with a TLS check turned off as well, that is the step's
    credentials. remote.<name>.proxy outranks http.proxy, and a URL-specific http.<url>.proxy outranks a
    general one, so each is planted on its own."""
    repo = repo_on_github(tmp_path, URL)
    git(repo, "config", planted, "http://127.0.0.1:9")
    said = curl_said(repo, offline(both_ways(tmp_path, as_who), URL), "ls-remote", "origin")
    assert (
        "Trying 127.0.0.1:443" in said and "Trying 127.0.0.1:9" not in said
    )  # straight to (stand-in) GitHub
    plain = curl_said(repo, offline(dict(os.environ), URL), "ls-remote", "origin")
    assert "Trying 127.0.0.1:9" in plain  # the control: plain git goes through the planted proxy


@pytest.mark.parametrize("as_who", ["you", "the bot"])
async def test_a_publish_step_resolves_github_without_a_list_a_model_planted(
    tmp_path: Path, own_git: Path, as_who: str
) -> None:
    repo = repo_on_github(tmp_path, URL)
    git(repo, "config", "http.curloptResolve", "github.com:443:127.0.0.3")  # where a listener would wait
    said = curl_said(repo, offline(both_ways(tmp_path, as_who), URL), "ls-remote", "origin")
    assert "127.0.0.3" not in said and "Added github.com:443:127.0.0.1 to DNS cache" in said
    plain = curl_said(repo, offline(dict(os.environ), URL), "ls-remote", "origin")
    assert "127.0.0.3" in plain  # the control


@pytest.mark.parametrize("as_who", ["you", "the bot"])
def test_a_publish_step_checks_tls_whatever_a_model_planted(
    tmp_path: Path, own_git: Path, as_who: str
) -> None:
    repo = repo_on_github(tmp_path, URL)
    git(repo, "config", "http.sslVerify", "false")
    verify = ["config", "--get-urlmatch", "http.sslverify", URL]
    pinned = git_in(repo, both_ways(tmp_path, as_who), *verify).stdout
    plain = git_in(repo, dict(os.environ), *verify).stdout
    assert (pinned, plain) == ("true\n", "false\n")  # plain git: the control


def with_alternates(tmp_path: Path, marks: Path) -> Path:
    """A clone whose origin (a local bare repository) has moved on since, with an objects/info/alternates
    and a core.alternateRefsCommand planted: git runs that command while it fetches."""
    remote, seed, alt = tmp_path / "alt-remote.git", tmp_path / "alt-seed", tmp_path / "alt.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    for name in ("one", "two"):
        (seed / name).write_text(name)
        git(seed, "add", name)
        git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
        git(seed, "push", "-q", str(remote), "main")
        if name == "one":
            git(tmp_path, "clone", "-q", str(remote), str(tmp_path / "alt-repo"))
            git(tmp_path, "clone", "-q", "--bare", str(remote), str(alt))
    repo = tmp_path / "alt-repo"
    (repo / ".git/objects/info/alternates").write_text(f"{alt / 'objects'}\n")
    said = 'echo "alternateRefsCommand GH_TOKEN=$GH_TOKEN SSH_AUTH_SOCK=$SSH_AUTH_SOCK"'
    git(repo, "config", "core.alternateRefsCommand", f"{said} >> {shlex.quote(str(marks))}; true")
    return repo


@pytest.mark.parametrize("as_who", ["you", "the bot"])
async def test_a_publish_steps_fetch_runs_no_alternate_refs_command_a_model_planted(
    tmp_path: Path,
    local_remotes: None,
    own_git: Path,
    marks: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_who: str,
) -> None:
    """It would run with the step's environment: your SSH agent, or the bot's token. The bot fetches only
    over HTTPS, so a local remote stands in for GitHub's (the alternates are read the same way)."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/the-persons-agent.sock")
    repo = with_alternates(tmp_path, marks)
    env = both_ways(tmp_path, as_who)
    if as_who == "the bot":
        env["GIT_ALLOW_PROTOCOL"] = "https:file"
    assert git_in(repo, env, "fetch", "-q", "origin", "main").returncode == 0
    assert logged(marks) == []
    git_in(repo, dict(os.environ), "fetch", "-q", "origin", "main")  # the control: plain git runs it
    assert "alternateRefsCommand GH_TOKEN= SSH_AUTH_SOCK=/tmp/the-persons-agent.sock" in logged(marks)


async def test_the_trunk_fetch_names_the_one_ref_it_updates_whatever_refspec_a_model_configured(
    tmp_path: Path, local_remotes: None, own_git: Path
) -> None:
    """A model can plant remote.origin.fetch: a refspec that maps nothing leaves origin/main where it was, and
    a story would build on that; one that maps the trunk onto a local branch has git move that branch as it
    fetches. The fetch names its one source and destination, and --refmap= has git use no configured one."""
    remote, seed, repo = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "repo"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    for name in ("one", "two"):
        git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", name)
        git(seed, "push", "-q", str(remote), "main")
        if name == "one":
            git(tmp_path, "clone", "-q", str(remote), str(repo))
    stale, news = git(repo, "rev-parse", "origin/main"), git(remote, "rev-parse", "main")
    git(repo, "branch", "yours", stale)  # a branch of the person's
    git(repo, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/main:refs/heads/yours")
    git(repo, "config", "--add", "remote.origin.fetch", "+refs/heads/nothing:refs/remotes/origin/nothing")
    ran: list[list[str]] = []

    async def run(args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        ran.append(args)
        return await run_cmd(args, cwd, env)

    await Publisher(run=run).fetch(repo, "main")
    assert ran == [["git", "fetch", "-q", "--refmap=", "origin", "+refs/heads/main:refs/remotes/origin/main"]]
    assert git(repo, "rev-parse", "origin/main") == news  # the one ref it names, moved
    assert git(repo, "rev-parse", "yours") == stale  # and nothing else
    git(repo, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")  # the control
    assert git(repo, "rev-parse", "yours") == news  # plain git also moves what the planted refspec maps


def transports(tmp_path: Path, marks: Path) -> tuple[Path, Path]:
    """A repository with an ext:: remote (and protocol.ext.allow=always) and a git:// remote on this machine
    (and a core.gitProxy) planted: each names a command git runs itself, with the step's environment."""
    repo = repo_with_marker_tests(tmp_path)
    planted = run_script(tmp_path / "transport.sh", f'echo "ran $*" >> {shlex.quote(str(marks))}\nexit 1')
    git(repo, "config", "remote.viaext.url", f"ext::{planted} %S")
    git(repo, "config", "protocol.ext.allow", "always")
    git(repo, "config", "remote.viagit.url", "git://127.0.0.1:9/o/r.git")
    git(repo, "config", "core.gitProxy", str(planted))
    return repo, planted


async def test_as_you_git_uses_no_transport_but_ssh_and_https(
    tmp_path: Path, own_git: Path, marks: Path
) -> None:
    repo, _ = transports(tmp_path, marks)
    env = yours()
    assert env["GIT_ALLOW_PROTOCOL"] == "ssh:https"  # SSH still works (tests above); local remotes do not
    for remote, transport in (("viaext", "ext"), ("viagit", "git")):
        out = git_in(repo, env, "ls-remote", remote)
        assert out.returncode != 0 and f"transport '{transport}' not allowed" in out.stderr
    assert logged(marks) == []
    for remote in ("viaext", "viagit"):  # the control: plain git runs both
        git_in(repo, dict(os.environ), "ls-remote", remote)
    assert len(logged(marks)) == 2


@pytest.mark.parametrize("as_who", ["you", "the bot"])
async def test_a_publish_step_runs_no_git_proxy_a_model_planted_even_where_git_is_allowed(
    tmp_path: Path, own_git: Path, marks: Path, as_who: str
) -> None:
    """core.gitProxy is the first one git reads, so only GIT_PROXY_COMMAND (empty) outranks a planted one."""
    repo, _ = transports(tmp_path, marks)
    env = {**both_ways(tmp_path, as_who), "GIT_ALLOW_PROTOCOL": "git"}
    assert env["GIT_PROXY_COMMAND"] == ""
    out = git_in(repo, env, "ls-remote", "viagit")  # straight to 127.0.0.1:9, where nothing listens
    assert out.returncode != 0 and logged(marks) == []


# ---- story check as the bot: where git would really fetch and push (fix round 1, M1) ----

ELSEWHERE = (
    "a url.<base>.insteadOf or pushInsteadOf (yours or the repository's) or a remote.origin.pushurl sends it "
    "there, and the bot fetches and pushes only over HTTPS, and only to o/r"
)


@pytest.mark.parametrize(
    ("rule", "said"),
    [
        ('[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n', "fetch from git@github.com:o/r.git"),
        (
            '[url "git@github.com:"]\n\tpushInsteadOf = https://github.com/\n',
            "push to git@github.com:o/r.git",
        ),
    ],
)
async def test_check_as_the_bot_names_a_rule_of_yours_that_sends_github_over_ssh(
    tmp_path: Path, own_git: Path, rule: str, said: str
) -> None:
    """A common global rule: the bot's git reads your global configuration too, and cannot use SSH."""
    own_git.write_text(rule)
    repo = repo_on_github(tmp_path, URL)
    found = await Publisher(run=Recorded(), app=fake_app(tmp_path, FakeGitHub())).check(repo)
    assert f"the remote 'origin' would {said}, not https://github.com/o/r: {ELSEWHERE}" in found.problems


async def test_check_as_the_bot_names_a_pushurl_a_model_planted(tmp_path: Path, own_git: Path) -> None:
    repo = repo_on_github(tmp_path)
    git(repo, "config", "remote.origin.pushurl", "https://me:hunter2@github.com/evil/other.git")
    found = await Publisher(run=Recorded(), app=fake_app(tmp_path, FakeGitHub())).check(repo)
    shown = "https://***@github.com/evil/other.git"  # the password masked
    assert found.problems == [
        f"the remote 'origin' would push to {shown}, not https://github.com/o/r: {ELSEWHERE}"
    ]
    assert found.notes == ["publishing to o/r as airlie-bot-test[bot]"]  # the app itself is fine


@pytest.mark.parametrize("planted", ["fetch.recurseSubmodules", "submodule.recurse"])
async def test_a_publish_steps_fetch_does_not_go_into_a_submodule(
    tmp_path: Path, local_remotes: None, own_git: Path, marks: Path, planted: str
) -> None:
    """A submodule has a configuration and a remote of its own, out of reach of the step's checks: a planted
    recursion would have its git fetch from there, with the step's environment. Here its remote is on this
    machine with a planted uploadpack, which git runs itself."""
    allow_file = ["-c", "protocol.file.allow=always"]  # git's default keeps local submodules out
    sub_remote = tmp_path / "sub.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(sub_remote))
    seed = tmp_path / "sub-seed"
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "s")
    git(seed, "push", "-q", str(sub_remote), "main")
    remote = tmp_path / "super.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    repo = repo_with_marker_tests(tmp_path)
    git(repo, *allow_file, "submodule", "add", "-q", str(sub_remote), "sub")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "sub")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "HEAD:main")
    receiver = run_script(
        tmp_path / "upload.sh", f"echo 'submodule uploadpack' >> {shlex.quote(str(marks))}\nexit 1"
    )
    git(repo / "sub", "config", "remote.origin.uploadpack", str(receiver))
    git(repo, "config", planted, "true" if planted == "submodule.recurse" else "yes")
    git_in(repo, yours(), *allow_file, "fetch", "-q", "origin")
    assert logged(marks) == []
    git_in(repo, dict(os.environ), *allow_file, "fetch", "-q", "origin")  # the control: plain git goes in
    assert logged(marks) == ["submodule uploadpack"]


def test_your_settings_for_one_repository_can_come_from_an_include_for_its_directory(
    tmp_path: Path, own_git: Path
) -> None:
    """The way to keep a per-repository deploy key (a core.sshCommand or credential.helper of the repository's
    own is not used): a file your global configuration includes for that repository's directory."""
    repo = repo_with_marker_tests(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    just_here = tmp_path / "gitconfig-project"
    just_here.write_text("[core]\n\tsshCommand = ssh -i ~/.ssh/deploy-key\n[credential]\n\thelper = store\n")
    own_git.write_text(f'[includeIf "gitdir:{repo.resolve()}/"]\n\tpath = {just_here}\n')
    assert PersonGit.read(repo) == PersonGit("ssh -i ~/.ssh/deploy-key", (("credential.helper", "store"),))
    assert PersonGit.read(other) == PersonGit()  # another repository: not included


# ---- as you: no remote on this machine (fix round 2, I1) ----


@pytest.mark.parametrize(
    ("where", "command", "action"),
    [
        ("remote.origin.pushurl", "remote.origin.receivepack", ["push", "origin", "HEAD:refs/heads/layer"]),
        ("remote.origin.url", "remote.origin.uploadpack", ["fetch", "origin"]),
    ],
)
def test_as_you_a_remote_on_this_machine_is_refused_so_its_planted_command_never_runs(
    tmp_path: Path,
    own_git: Path,
    marks: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
    command: str,
    action: list[str],
) -> None:
    """For a remote that is a path, git runs the receivepack or uploadpack the repository's configuration
    names itself, with your environment (your SSH agent), and takes the first one it reads: no setting given
    on the command line can outrank a planted one. Publishing as you allows SSH and HTTPS remotes only."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/the-persons-agent.sock")
    repo = repo_on_github(tmp_path)  # the story's remote, as written
    local = tmp_path / "local.git"
    git(tmp_path, "init", "-q", "--bare", str(local))
    git(repo, "config", where, str(local))
    planted = run_script(
        tmp_path / "planted.sh",
        f'echo "ran SSH_AUTH_SOCK=$SSH_AUTH_SOCK" >> {shlex.quote(str(marks))}\nexit 1',
    )
    git(repo, "config", command, str(planted))
    env = yours()
    refused = git_in(repo, env, *action).stderr
    assert "transport 'file' not allowed" in refused and logged(marks) == []
    assert env["GIT_ALLOW_PROTOCOL"] == "ssh:https"
    git_in(repo, dict(os.environ), *action)  # the control: plain git runs it, with your agent
    assert logged(marks) == ["ran SSH_AUTH_SOCK=/tmp/the-persons-agent.sock"]
