"""What a process that runs model-chosen or model-written code sees (airlie.subproc.agent_env): no credential,
an empty gh configuration, no git credential helper and no prompt, wherever airlie starts one. Real
subprocesses, under a git configuration of the test's own (no system file; a global file in tmp_path) whose
credential helpers would answer."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

from airlie import subproc
from airlie.engine import FakeEngine, ToolCall
from airlie.evals import EvalTask
from airlie.orchestrator import ParallelAgentTeamOrchestrator
from airlie.repo_map import build_repo_map
from airlie.subproc import (
    CREDENTIALS,
    NO_PROMPTS,
    NO_SSH,
    agent_env,
    gh_config_dir,
    url_credential_helpers,
    with_git_config,
    without_hooks,
)
from airlie.teams import TeamResult
from airlie.teams.worktree import commit_wip
from airlie.tools import ToolContext, ToolRegistry
from airlie.topology.studio import StudioOrchestrator
from tests.unit.proc_helpers import eventually, running, unique_sleep
from tests.unit.test_fork_orchestrator import cfg_for as fork_cfg
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_studio_orchestrator import cfg_for as studio_cfg

URL = "https://example.invalid"
HELPER = "!f() { echo username=planted; echo password=planted-secret; }; f"


@pytest.fixture
def planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every variable the agent environment leaves out, set; and a git configuration of the test's own (no
    system file, the global file this returns) with a credential helper that answers, for any host and
    for URL."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        f'[credential]\n\thelper = "{HELPER}"\n[credential "{URL}"]\n\thelper = "{HELPER}"\n'
    )
    # start from an unsanitized environment even when this suite runs inside an agent's (a story layer's
    # gate does: airlie sets these on every process a model's code starts)
    for name in (*NO_PROMPTS, *NO_SSH, "GH_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name in [
        n
        for n in os.environ
        if n == "GIT_CONFIG_COUNT" or n.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    ]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    for name in CREDENTIALS:
        monkeypatch.setenv(name, f"planted-{name.lower()}")
    url_credential_helpers.cache_clear()
    yield gitconfig
    url_credential_helpers.cache_clear()


def credential_fill(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """`git credential fill` for URL, as a process with `env` runs it."""
    return subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=example.invalid\n\n",
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def helpers(env: dict[str, str], cwd: Path, url: str = URL) -> list[str]:
    """The credential helpers git would ask for `url` under `env`: each value of credential.helper and of
    credential.<url>.helper, in the order git reads them, where an empty one empties the list."""
    out = subprocess.run(
        ["git", "config", "--get-regexp", r"^credential\..*helper$"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    listed: list[str] = []
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key in ("credential.helper", f"credential.{url}.helper"):
            listed = [*listed, value] if value else []
    return listed


def config_pairs(env: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    ]


def test_the_planted_helpers_answer_a_process_with_airlies_own_environment(
    planted: Path, tmp_path: Path
) -> None:
    """The control: without the agent environment, the helpers hand out a password."""
    out = credential_fill(dict(os.environ), tmp_path)
    assert out.returncode == 0 and "password=planted-secret" in out.stdout
    assert helpers(dict(os.environ), tmp_path) == [HELPER, HELPER]


def test_the_agent_environment_drops_the_credentials_and_resets_each_helper(planted: Path) -> None:
    env = agent_env()
    assert [name for name in CREDENTIALS if name in env] == []
    assert {name: env[name] for name in NO_PROMPTS} == NO_PROMPTS
    assert NO_PROMPTS == {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false", "SSH_ASKPASS": "false"}
    assert {name: env[name] for name in NO_SSH} == NO_SSH == {"GIT_SSH_COMMAND": "false"}
    assert env["GH_CONFIG_DIR"] == str(gh_config_dir())
    assert env["PATH"] == os.environ["PATH"] and env["GIT_CONFIG_GLOBAL"] == str(planted)  # the rest is kept
    # the URL-specific helper first, then the plain one: each set to empty empties the list of helpers
    assert config_pairs(env) == [(f"credential.{URL}.helper", ""), ("credential.helper", "")]


def test_the_agent_environment_has_no_active_virtual_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A story tick runs under `uv run` in airlie's checkout, whose VIRTUAL_ENV the coder's shell inherited:
    every `uv run` in the story's worktree then warned that it does not match and is ignored (AIR-7 layer 02,
    2026-10-18: the coder piped each command through `grep -v VIRTUAL_ENV`, losing exit codes)."""
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/airlie/.venv")
    assert "VIRTUAL_ENV" not in agent_env()


def test_the_agent_path_in_a_worktree_puts_airlies_own_interpreter_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AIR-30 layer 02, 2026-10-19: `uv run airlie story tick` put airlie's checkout's .venv/bin first on
    PATH, so the coder's own `python -m pytest` in its worktree ran airlie's interpreter and tested the code
    of main, not its layer (a field layer 01 added was "not permitted"). Those bins go last on PATH."""
    import sys

    own = str(Path(sys.prefix) / "bin")
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/airlie/.venv")
    monkeypatch.setenv("PATH", f"{own}:/somewhere/airlie/.venv/bin:/usr/bin:/bin")
    assert agent_env(tmp_path)["PATH"] == f"/usr/bin:/bin:{own}:/somewhere/airlie/.venv/bin"
    assert agent_env()["PATH"] == os.environ["PATH"]  # airlie's own commands (evals, publishing) keep theirs


def test_the_agent_path_starts_with_the_worktrees_own_venv_when_it_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert agent_env(tmp_path)["PATH"] == "/usr/bin:/bin"  # no .venv yet: nothing to put first
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    assert agent_env(tmp_path)["PATH"] == f"{tmp_path / '.venv' / 'bin'}:/usr/bin:/bin"


def test_url_credential_helpers_lists_each_url_specific_key_once(planted: Path) -> None:
    planted.write_text(
        planted.read_text()
        + f'[credential "{URL}"]\n\thelper = another\n[credential "https://two.invalid"]\n\thelper = x\n'
    )
    assert url_credential_helpers() == (f"credential.{URL}.helper", "credential.https://two.invalid.helper")


def test_without_git_there_is_no_url_specific_helper_to_reset(
    planted: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))  # no git on it
    assert url_credential_helpers() == ()
    assert config_pairs(agent_env()) == [("credential.helper", "")]


def test_git_configuration_is_added_after_the_entries_already_in_the_environment(tmp_path: Path) -> None:
    base = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "airlie.a",
        "GIT_CONFIG_VALUE_0": "kept",
        "GIT_CONFIG_KEY_1": "airlie.b",
        "GIT_CONFIG_VALUE_1": "overridden",
        "OTHER": "x",
    }
    env = with_git_config(base, [("airlie.b", "later"), ("airlie.c", "")])
    assert env == {
        **base,
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_2": "airlie.b",
        "GIT_CONFIG_VALUE_2": "later",
        "GIT_CONFIG_KEY_3": "airlie.c",
        "GIT_CONFIG_VALUE_3": "",
    }
    assert base["GIT_CONFIG_COUNT"] == "2"  # a copy

    def value(key: str) -> str:
        cmd = ["git", "config", "--get", key]
        return subprocess.run(
            cmd, cwd=tmp_path, env={**os.environ, **env}, capture_output=True, text=True
        ).stdout

    assert (value("airlie.a"), value("airlie.b")) == ("kept\n", "later\n")  # what git makes of them
    assert with_git_config({}, [("airlie.c", "1")]) == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "airlie.c",
        "GIT_CONFIG_VALUE_0": "1",
    }
    for bogus in ("two", "-1", "²"):  # git refuses each of these counts: the entries start again at 0
        assert with_git_config({"GIT_CONFIG_COUNT": bogus}, [("airlie.c", "1")])["GIT_CONFIG_COUNT"] == "1"


def test_without_hooks_keeps_the_credentials_and_adds_its_settings_last() -> None:
    env = without_hooks(
        {"GH_TOKEN": "t", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"}
    )
    assert env["GH_TOKEN"] == "t"
    assert config_pairs(env) == [
        ("a.b", "c"),
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("core.useReplaceRefs", "false"),  # no object a model replaced with `git replace`
    ]


def test_the_gh_directory_is_one_private_empty_directory_per_process() -> None:
    first = gh_config_dir()
    assert gh_config_dir() == first and first.is_dir() and os.listdir(first) == []
    assert first.name.startswith("airlie-gh-") and stat.S_IMODE(first.stat().st_mode) == 0o700
    shutil.rmtree(first)  # gone from under it: it makes another
    second = gh_config_dir()
    assert second != first and second.is_dir() and os.listdir(second) == []


def test_only_the_process_that_made_the_gh_directory_removes_it(tmp_path: Path) -> None:
    made = tmp_path / "gh"
    made.mkdir()
    subproc._remove(os.getpid() + 1, made)  # a forked child at its exit: not its directory
    assert made.is_dir()
    subproc._remove(os.getpid(), made)
    assert not made.exists()
    subproc._remove(os.getpid(), made)  # already gone (removed by hand): no error at exit


def test_the_gh_directory_goes_when_its_process_exits_and_not_when_a_fork_of_it_does() -> None:
    script = (
        "import os, sys\n"
        "from airlie.subproc import gh_config_dir\n"
        "made = gh_config_dir()\n"
        "if os.fork() == 0:\n"
        "    sys.exit(0)  # a forked worker ends and runs its exit handlers\n"
        "os.wait()\n"
        "print(made, made.is_dir())\n"
    )
    import airlie

    assert gh_config_dir().is_dir()  # called here too: a mutation run then knows this test covers it
    here = str(Path(airlie.__file__).parents[1])  # this airlie, the one under test
    env = {**os.environ, "PYTHONPATH": here}
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    made, alive = out.stdout.split()
    assert alive == "True" and not Path(made).exists(), out.stderr


async def test_killing_a_group_that_is_already_gone_is_no_error() -> None:
    proc = await asyncio.create_subprocess_exec(
        "true", stdout=asyncio.subprocess.PIPE, start_new_session=True
    )
    await asyncio.wait_for(proc.wait(), 10)  # reaped: there is no group left to kill
    await asyncio.wait_for(subproc.kill_group(proc), 10)


async def test_draining_stops_when_a_process_outside_the_group_holds_the_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command can start something in a session of its own, out of reach of killpg, that keeps the
    command's output pipe open: the drain gives up after DRAIN_S instead of waiting for it."""
    monkeypatch.setattr(subproc, "DRAIN_S", 0.5)
    sleeper = unique_sleep()
    escape = (
        f"import subprocess, time; subprocess.Popen({sleeper.split()!r}, start_new_session=True); "
        "time.sleep(30)"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", escape, stdout=asyncio.subprocess.PIPE, start_new_session=True
    )
    try:
        assert await eventually(lambda: running(f"^{sleeper}"))
        started = time.monotonic()
        await asyncio.wait_for(subproc.kill_group(proc), 10)
        assert time.monotonic() - started < 3 and await eventually(lambda: proc.returncode is not None)
    finally:
        subprocess.run(["pkill", "-f", f"^{sleeper}"], capture_output=True)
        await asyncio.wait_for(proc.wait(), 10)  # the pipe closes with the sleep: nothing is left open


# ---- every place that starts one ----


def dump_command(tmp_path: Path) -> tuple[str, Path]:
    """A shell command that writes the environment it runs with to a JSON file; and that file."""
    script = tmp_path / "dump_env.py"
    script.write_text("import json, os, sys\njson.dump(dict(os.environ), open(sys.argv[1], 'w'))\n")
    out = tmp_path / "env.json"
    return shlex.join([sys.executable, str(script), str(out)]), out


def git_on_path_runs_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: str) -> None:
    """`git` on PATH becomes a script that runs `cmd` and then the real git."""
    real = shutil.which("git")
    assert real is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(f'#!/bin/sh\n{cmd}\nexec {shlex.quote(real)} "$@"\n')
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


Site = Callable[[Path, Path, str, pytest.MonkeyPatch], Awaitable[object]]


def tool(name: str, args: dict[str, object], test_cmd: str | None = None) -> Site:
    async def call(tmp_path: Path, repo: Path, cmd: str, monkeypatch: pytest.MonkeyPatch) -> object:
        ctx = ToolContext(repo, 30.0, 2000, test_cmd or cmd)
        return await ToolRegistry.builtin([name], ctx).call(ToolCall("1", name, json.dumps(args)))

    return call


async def git_tool(tmp_path: Path, repo: Path, cmd: str, monkeypatch: pytest.MonkeyPatch) -> object:
    git_on_path_runs_first(tmp_path, monkeypatch, cmd)
    return await tool("git", {"args": ["status"]})(tmp_path, repo, cmd, monkeypatch)


async def shell_tool(tmp_path: Path, repo: Path, cmd: str, monkeypatch: pytest.MonkeyPatch) -> object:
    return await tool("shell", {"cmd": cmd})(tmp_path, repo, cmd, monkeypatch)


async def run_tests_tool(tmp_path: Path, repo: Path, cmd: str, monkeypatch: pytest.MonkeyPatch) -> object:
    return await tool("run_tests", {})(tmp_path, repo, cmd, monkeypatch)


async def studio_gate(tmp_path: Path, repo: Path, cmd: str, monkeypatch: pytest.MonkeyPatch) -> object:
    orch = StudioOrchestrator(studio_cfg(tmp_path), FakeEngine({}))
    orch.worktree, orch.runs = repo, {"done": None}  # type: ignore[dict-item]  # the gate runs after a subtask
    return await orch._tests_pass(cmd)


async def fork_gate(tmp_path: Path, repo: Path, cmd: str, monkeypatch: pytest.MonkeyPatch) -> object:
    orch = ParallelAgentTeamOrchestrator(fork_cfg(tmp_path), FakeEngine({}))
    (orch.run_dir.path / "t").mkdir()
    return await orch._outcome(TeamResult("t", True, "done", "", 0, 0, 0, 0.0), cmd)


async def eval_grade(tmp_path: Path, repo: Path, cmd: str, monkeypatch: pytest.MonkeyPatch) -> object:
    return EvalTask("t", "python", "x", cmd).grade(repo)


async def airlies_own_git(tmp_path: Path, repo: Path, cmd: str, monkeypatch: pytest.MonkeyPatch) -> object:
    git_on_path_runs_first(tmp_path, monkeypatch, cmd)
    return commit_wip(repo, "wip")


async def repo_map_git(tmp_path: Path, repo: Path, cmd: str, monkeypatch: pytest.MonkeyPatch) -> object:
    git_on_path_runs_first(tmp_path, monkeypatch, cmd)
    return build_repo_map(repo, test_cmd=None, max_chars=1000, depth=2, cache_dir=None)


SITES: dict[str, Site] = {
    "shell tool": shell_tool,
    "run_tests tool": run_tests_tool,
    "git tool": git_tool,
    "studio test gate": studio_gate,
    "fork test gate": fork_gate,
    "eval grader": eval_grade,
    "airlie's worktree git": airlies_own_git,
    "repo map's git": repo_map_git,
}


@pytest.mark.parametrize("site", list(SITES))
async def test_a_process_that_runs_model_code_has_no_credential_and_cannot_get_one(
    planted: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cmd, dumped = dump_command(tmp_path)
    url_credential_helpers()  # read now, by the real git: a git on PATH that runs `cmd` would also dump
    await asyncio.wait_for(SITES[site](tmp_path, repo, cmd, monkeypatch), 60)
    env = json.loads(dumped.read_text())
    assert [name for name in CREDENTIALS if name in env] == []
    assert os.listdir(env["GH_CONFIG_DIR"]) == []
    assert helpers(env, tmp_path) == []
    started = time.monotonic()
    fill = credential_fill(env, tmp_path)  # what the process gets when it asks git for a password
    assert fill.returncode != 0 and "planted" not in fill.stdout + fill.stderr
    assert "terminal prompts disabled" in fill.stderr and time.monotonic() - started < 5
