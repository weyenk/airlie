"""How airlie starts the processes that must not act as the person.

`agent_env` is the environment of every process that runs code a model chose or wrote: the coder's shell,
run_tests and git tools, and the test gates. It holds no GitHub, SSH or Jira credential, so a model cannot
`gh pr merge` or `git push` with the person's identity through the environment and the normal tools. It is
not an OS sandbox: the process still runs as the person's macOS user, and a model that deliberately goes
after what is stored for that user (the keychain, ~/.ssh) is not stopped by it.

airlie's own git runs no repository hook and no fsmonitor (`NO_HOOKS`): a model can plant either in the
repository it works in, and git would run them inside airlie's commit or push. `kill_group` stops a command
and everything it started."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path

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

NO_PROMPTS = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false", "SSH_ASKPASS": "false"}
"""git and ssh never prompt for a password: in an agent's environment, and in publishing's (both ways)."""
NO_SSH = {"GIT_SSH_COMMAND": "false"}
"""git over SSH (git@github.com:...) fails at once instead of reaching a key. It outranks core.sshCommand."""

NO_HOOKS = (("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"))
"""airlie's own git: no hook runs (there is none under /dev/null) and no fsmonitor."""
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


def agent_env() -> dict[str, str]:
    """The environment for code a model chose or wrote: airlie's own without CREDENTIALS, gh pointed at an
    empty configuration directory, git and ssh unable to prompt (NO_PROMPTS) or to use SSH (NO_SSH), and every
    git credential helper reset. An empty `credential.helper` empties the list of helpers, and so does each
    `credential.<url>.helper` set to empty."""
    env = {name: value for name, value in os.environ.items() if name not in CREDENTIALS}
    env.update(NO_PROMPTS)
    env.update(NO_SSH)
    env["GH_CONFIG_DIR"] = str(gh_config_dir())
    resets = [*url_credential_helpers(), "credential.helper"]
    return with_git_config(env, [(key, "") for key in resets])


def without_hooks(env: Mapping[str, str]) -> dict[str, str]:
    """`env` with NO_HOOKS: for airlie's own git and gh that publish (with the person's credentials, or a
    GitHub App's token), which must run nothing a model planted in the repository."""
    return with_git_config(env, NO_HOOKS)


async def kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL a command and everything it started (start it with `start_new_session=True`: its own process
    group), then read its pipes until they close, for DRAIN_S at most. A command left running would go on
    (pushing, or holding the pipes), and output that nobody reads keeps the command from being reaped."""
    with contextlib.suppress(ProcessLookupError):  # nothing of it is left
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):  # something that left the group still holds a pipe
        await asyncio.wait_for(proc.communicate(), DRAIN_S)
