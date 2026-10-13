"""One git worktree per team/branch under the run directory, each on its own git branch; fork helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


_ENV = ["-c", "user.name=airlie", "-c", "user.email=airlie@local"]


def add_worktree(repo: Path, path: Path, *, branch: str, start_point: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    base = start_point or _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
    return path


def commit_wip(worktree: Path, message: str) -> str:
    """Commit everything in the worktree (untracked included; empty allowed) and return the new sha."""
    _git(worktree, *_ENV, "add", "-A")
    _git(worktree, *_ENV, "commit", "-q", "--allow-empty", "-m", message)
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def diff_last_commit(worktree: Path, max_chars: int) -> str:
    """The last commit's diff (what the branch changed since the previous fork base), capped."""
    out = subprocess.run(
        ["git", "show", "--format=", "--no-color", "HEAD"], cwd=worktree, capture_output=True, text=True
    ).stdout
    return out[:max_chars]


def remove_worktree(repo: Path, path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)], cwd=repo, capture_output=True, text=True
    )
    subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True, text=True)
