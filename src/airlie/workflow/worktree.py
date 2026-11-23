"""Per-workdir run lock: one workflow run at a time on a given directory.

`start_workflow` claims `workdir` with an `O_EXCL` lock file before the runner starts and releases it
when the run ends. This module is that lock only — it does not create git worktrees. Callers that need
an isolated checkout still use `airlie.teams.worktree` (or pass a worktree path as `workdir`).
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
