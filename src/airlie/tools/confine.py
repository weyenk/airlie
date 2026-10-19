"""Commands a model chose, or code it wrote, can write only where its work is: an OS sandbox (macOS Seatbelt,
`sandbox-exec`) that refuses every file write outside an allow list.

airlie's environment for such commands (`airlie.subproc.agent_env`) holds no credential, but the process
still ran as the person, so a shell command could change anything the person can. On 2026-10-19 a studio
coder whose `python` failed (a stale asdf shim) re-pointed `~/.asdf/shims/python*`, made directories under
`/opt/homebrew/Cellar`, and pip-installed pytest into the person's own Python, to get its tests to run.

Writable: the worktree, the repository's git directory (a worktree's commits and refs live in the main
repository's `.git`), the temporary directories (`/private/tmp`, and the per-user ones macOS gives every
program: `mktemp` and clang use them whatever TMPDIR says), uv's cache and Python installs (`uv sync` and
`uv run` in a worktree need them), `/dev` (null, tty, the pipes), and what `tools.writable` adds. Reads, the
network and running programs are not restricted: this is about not changing the machine, not about secrets
(see `agent_env`).

A write outside fails as the OS fails it ("Operation not permitted"); the command's output then gets a line
saying why, so a model does not go looking for another way round. Off macOS (no `sandbox-exec`) commands run
as before; in a process already in a sandbox, under that one (`available`)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from functools import cache
from pathlib import Path

from airlie.subproc import agent_env

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
REFUSED = "Operation not permitted"
NOTE = (
    "[airlie: this command may write only inside the repository's worktree (and temporary and cache "
    "directories); a write anywhere else on the machine is refused. Do not try to change the machine: "
    "say what the environment lacks.]"
)

_CS_DARWIN_USER_TEMP_DIR = 65537
_CS_DARWIN_USER_CACHE_DIR = 65538
"""<unistd.h>'s confstr names for the per-user temporary and cache directories (`getconf DARWIN_USER_…`):
Python has no name for them."""


@cache
def available() -> bool:
    """True when commands can be confined: macOS with sandbox-exec, in a process not already in a sandbox. A
    sandbox cannot be applied inside another (sandbox-exec exits 71: a story's gate in airlie's own
    repository runs airlie's tool tests inside the coder's run_tests), and the one already applied confines
    what this process starts anyway. Probed once per process."""
    if sys.platform != "darwin" or not os.access(SANDBOX_EXEC, os.X_OK):
        return False
    try:
        probe = subprocess.run(
            [SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"], timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


@cache
def git_common_dir(worktree: Path) -> Path | None:
    """The git directory a worktree's objects and refs are written to: the main repository's `.git` for a
    linked worktree, the worktree's own `.git` otherwise. None outside a repository."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=worktree,
            env=agent_env(),  # a git on PATH is as untrusted as the worktree: no credential for it either
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = out.stdout.strip()
    return Path(line) if out.returncode == 0 and line else None


@cache
def _darwin_dir(name: int) -> Path | None:
    """One of macOS's per-user directories (`/var/folders/…/T` or `…/C`)."""
    try:
        found = os.confstr(name)
    except (ValueError, OSError):
        return None
    return Path(found) if found else None


def _uv_dirs() -> list[Path]:
    home = Path.home()
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return [
        Path(os.environ.get("UV_CACHE_DIR") or cache_home / "uv"),
        Path(os.environ.get("UV_PYTHON_INSTALL_DIR") or data_home / "uv" / "python"),
    ]


def writable_roots(worktree: Path, extra: Sequence[Path] = ()) -> list[Path]:
    """Every directory a confined command in `worktree` may write under, resolved (Seatbelt matches real
    paths: /tmp is /private/tmp), without repeats."""
    candidates: list[Path | None] = [
        worktree,
        git_common_dir(worktree.resolve()),
        Path("/private/tmp"),
        _darwin_dir(_CS_DARWIN_USER_TEMP_DIR),
        _darwin_dir(_CS_DARWIN_USER_CACHE_DIR),
        *_uv_dirs(),
        Path("/dev"),
        *extra,
    ]
    out: list[Path] = []
    for p in candidates:
        if p is None:
            continue
        real = Path(os.path.realpath(p.expanduser()))
        if real != Path("/") and real not in out:
            out.append(real)
    return out


def profile(n: int) -> str:
    """A Seatbelt profile that allows everything but file writes outside the parameters W0 … W{n-1}."""
    allowed = " ".join(f'(subpath (param "W{i}"))' for i in range(n))
    return f"(version 1)\n(allow default)\n(deny file-write*)\n(allow file-write* {allowed})\n"


def argv(command: Sequence[str], roots: Sequence[Path]) -> list[str]:
    """`command` run by sandbox-exec under `profile`, the writable roots passed as parameters (no quoting
    of paths inside the profile)."""
    params = [arg for i, r in enumerate(roots) for arg in ("-D", f"W{i}={r}")]
    return [SANDBOX_EXEC, *params, "-p", profile(len(roots)), *command]


def explain(output: str, rc: int) -> str:
    """`output` with NOTE after it when the command failed on a refused write."""
    return f"{output.rstrip()}\n{NOTE}\n" if rc != 0 and REFUSED in output else output
