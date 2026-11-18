"""A run's git worktree and its lock: one run per workdir at a time.

The runner builds its repository in a git worktree under the run directory. This guards a workdir with a
lock file (one run at a time) and re-exports the git helpers the runner and CLI use. The Tart VMs and
container engines the old story pipeline drove are retired: a repository is only ever built in a worktree.
"""

from __future__ import annotations

import os
from pathlib import Path

LOCK_NAME = ".airlie-run.lock"

__all__ = ["LOCK_NAME", "acquire_lock", "lock_path", "release_lock"]


def lock_path(workdir: Path) -> Path:
    """The lock file for `workdir`."""
    return workdir / LOCK_NAME


def acquire_lock(workdir: Path) -> bool:
    """Claim `workdir` for a run; True if free, False if another run holds it.

    Atomic on one machine: O_EXCL means only the first creator of the file wins a race.
    """
    path = lock_path(workdir)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def release_lock(workdir: Path) -> None:
    """Give `workdir` back; idempotent."""
    lock_path(workdir).unlink(missing_ok=True)
