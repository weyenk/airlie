"""How airlie starts the processes that must not act as the person.

`agent_env` is the environment of every process that runs code a model chose or wrote: the coder's shell,
run_tests and git tools, and the test gates. It holds no GitHub, SSH or Jira credential, so a model cannot
`gh pr merge` or `git push` with the person's identity through the environment and the normal tools. The
tools also cannot write outside the worktree (airlie.tools.confine, an OS sandbox on macOS), but they can
still read as the person's macOS user: a model that deliberately goes after what is stored for that user (the
keychain, ~/.ssh, ~/.config/airlie/.env and the checkout's .env with the Jira token, the GitHub App's private
key) is not stopped by either.

airlie's own git runs no repository hook and no fsmonitor, and reads no object through `git replace`
(`NO_HOOKS`): a model can plant any of them in the repository it works in, and git would run a hook inside
airlie's commit or push, or show airlie a planted commit or file in place of the real one. `kill_group` stops
a command and everything it started."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Coroutine, Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

CREDENTIALS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "ATLASSIAN_API_KEY",
    "ATLASSIAN_EMAIL",
    "ATLASSIAN_BASE_URL",
    "ATLASSIAN_PROJECT_KEY",
    "AIRLIE_SERVE_API_KEY",
    "SSH_AUTH_SOCK",
)
"""Not in an agent's environment: gh's tokens, airlie's Jira settings, serve's API key and the SSH agent."""

OWN_PYTHON = ("VIRTUAL_ENV",)
"""Not in an agent's environment either: the virtual environment airlie itself runs in (`uv run airlie` sets
it to airlie's checkout). A story's worktree has its own, which `uv run` there finds by itself; with airlie's
in the environment, every `uv run` in the worktree warns that it does not match and is ignored, and the coder
pipes each command through `grep -v VIRTUAL_ENV`, losing its exit code (AIR-7 layer 02, 2026-10-18)."""

NO_PROMPTS = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false", "SSH_ASKPASS": "false"}
"""git and ssh never prompt for a password: in an agent's environment, and in publishing's (both ways)."""
NO_SSH = {"GIT_SSH_COMMAND": "false"}
"""git over SSH (git@github.com:...) fails at once instead of reaching a key. It outranks core.sshCommand."""

NO_HOOKS = (("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"), ("core.useReplaceRefs", "false"))
"""airlie's own git: no hook runs (there is none under /dev/null), no fsmonitor, and no refs/replace object
stands in for another (a model's `git replace` in any worktree reaches every other: refs are shared)."""
NO_HOOKS_ARGS = tuple(arg for key, value in NO_HOOKS for arg in ("-c", f"{key}={value}"))
"""NO_HOOKS as git's -c options."""

DRAIN_S = 5.0
"""How long `kill_group` reads what a killed command left in its pipes."""

_gh_dir: Path | None = None


def _remove(pid: int, path: Path) -> None:
    if os.getpid() == pid:  # a forked child inherits this exit handler; the directory is its parent's
        shutil.rmtree(path, ignore_errors=True)


def gh_config_dir() -> Path:
    """An empty directory only this user can open, made once per process and removed when it exits. As
    GH_CONFIG_DIR it leaves gh with no login, so gh never looks for a token in the keychain."""
    global _gh_dir
    if _gh_dir is None or not _gh_dir.is_dir():
        _gh_dir = Path(tempfile.mkdtemp(prefix="airlie-gh-"))
        atexit.register(_remove, os.getpid(), _gh_dir)
    return _gh_dir


def with_git_config(env: Mapping[str, str], pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """`env` plus `pairs` as git configuration given on the command line (GIT_CONFIG_COUNT with
    GIT_CONFIG_KEY_<n> and GIT_CONFIG_VALUE_<n>), after the entries `env` already holds: those stay, and
    these, read after them, win. It outranks the repository's own configuration, and every git a child
    starts (a hook's, gh-stack's) inherits it."""
    out = dict(env)
    count = out.get("GIT_CONFIG_COUNT", "")
    n = int(count) if count.isascii() and count.isdigit() else 0  # git refuses any other count anyway
    for key, value in pairs:
        out[f"GIT_CONFIG_KEY_{n}"] = key
        out[f"GIT_CONFIG_VALUE_{n}"] = value
        n += 1
    out["GIT_CONFIG_COUNT"] = str(n)
    return out


@cache
def url_credential_helpers() -> tuple[str, ...]:
    """The URL-specific helper keys (credential.<url>.helper) of the system and global git configuration,
    such as the one `gh auth setup-git` writes for github.com. Read once per process."""
    try:
        listed = subprocess.run(
            ["git", "config", "--includes", "--name-only", "--get-regexp", r"^credential\..+\.helper$"],
            cwd="/",  # outside any repository: the system and global configuration only
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):  # no git: nothing to reset
        return ()
    return tuple(dict.fromkeys(listed.splitlines()))


def agent_env(worktree: Path | None = None) -> dict[str, str]:
    """The environment for code a model chose or wrote: airlie's own without CREDENTIALS or OWN_PYTHON, gh
    pointed at an empty configuration directory, git and ssh unable to prompt (NO_PROMPTS) or to use SSH
    (NO_SSH), and every git credential helper reset. An empty `credential.helper` empties the list of helpers,
    and so does each `credential.<url>.helper` set to empty. With a `worktree` (a model's shell there), PATH
    moves the bin of airlie's own interpreter (the checkout's .venv that `uv run` put first) to the end and
    starts with the worktree's .venv/bin when there is one: a `python` or `pytest` the model runs there is
    the worktree's, not airlie's (AIR-30, 2026-10-19)."""
    dropped = (*CREDENTIALS, *OWN_PYTHON)
    env = {name: value for name, value in os.environ.items() if name not in dropped}
    if worktree is not None:  # a model's shell in a worktree; airlie's own commands keep their PATH
        env["PATH"] = _agent_path(os.environ.get("PATH", ""), os.environ.get("VIRTUAL_ENV"), worktree)
    env.update(NO_PROMPTS)
    env.update(NO_SSH)
    env["GH_CONFIG_DIR"] = str(gh_config_dir())
    resets = [*url_credential_helpers(), "credential.helper"]
    return with_git_config(env, [(key, "") for key in resets])


def _agent_path(path: str, virtual_env: str | None, worktree: Path | None) -> str:
    """`path` with `worktree/.venv/bin` first when that directory exists, and the bin of airlie's own
    interpreter (sys.prefix) or active virtual environment moved to the end: still found when the worktree
    has no tool of its own, never ahead of the worktree's."""
    own = {str(Path(sys.prefix) / "bin")}
    if virtual_env:
        own.add(str(Path(virtual_env) / "bin"))
    entries = [p for p in path.split(os.pathsep) if p]
    kept = [p for p in entries if p not in own] + [p for p in entries if p in own]
    if worktree is not None and (venv_bin := worktree / ".venv" / "bin").is_dir():
        kept.insert(0, str(venv_bin))
    return os.pathsep.join(kept)


def without_hooks(env: Mapping[str, str]) -> dict[str, str]:
    """`env` with NO_HOOKS: for airlie's own git and gh that publish (with the person's credentials, or a
    GitHub App's token), which must run nothing a model planted in the repository and read no object it
    replaced."""
    return with_git_config(env, NO_HOOKS)


async def started(starting: Coroutine[Any, Any, asyncio.subprocess.Process]) -> asyncio.subprocess.Process:
    """The process `starting` (an asyncio.create_subprocess_* call) makes. Anything that stops the wait for it
    (a cancel that lands while it is being made: the child already runs, asyncio connects its pipes after the
    fork) waits for it, kills it and all it started (`kill_group`), and goes on: else the caller never has it
    to kill (PR 105's CI, 2026-10-19: a cancelled gate's sleep outlived the test). A start that failed
    (OSError) has nothing to kill and is raised."""
    task = asyncio.ensure_future(starting)
    try:
        return await asyncio.shield(task)
    except BaseException:
        # it never started, so there is nothing to kill, and the wait raised the same OSError
        with contextlib.suppress(OSError):  # pragma: no mutate
            await kill_group(await task)
        raise


async def kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL a command and everything it started (start it with `start_new_session=True`: its own process
    group), then read its pipes until they close, for DRAIN_S at most. A command left running would go on
    (pushing, or holding the pipes), and output that nobody reads keeps the command from being reaped."""
    with contextlib.suppress(ProcessLookupError):  # nothing of it is left
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):  # something that left the group still holds a pipe
        await asyncio.wait_for(proc.communicate(), DRAIN_S)
