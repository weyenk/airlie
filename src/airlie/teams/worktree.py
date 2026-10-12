"""One git worktree per team under the run directory, each on its own branch."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def add_worktree(repo: Path, path: Path, *, branch: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), head)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)], cwd=repo, capture_output=True, text=True
    )
    subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True, text=True)
