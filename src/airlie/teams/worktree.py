"""One git worktree per team/branch under the run directory, each on its own git branch; fork helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


_ENV = ["-c", "user.name=airlie", "-c", "user.email=airlie@local"]


def add_worktree(repo: Path, path: Path, *, branch: str | None, start_point: str | None = None) -> Path:
    """A new worktree at `start_point` (default HEAD) on a new `branch`; `branch=None` detaches it, so
    read-only work leaves no branch behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    base = start_point or _git(repo, "rev-parse", "HEAD").stdout.strip()
    new = ["--detach"] if branch is None else ["-b", branch]
    _git(repo, "worktree", "add", "-q", *new, str(path), base)
    return path


def checkout_worktree(repo: Path, path: Path, branch: str) -> Path:
    """A worktree on an existing branch: `gh stack` runs there, never in the person's own checkout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(path), branch)
    return path


def rev_parse(repo: Path, ref: str) -> str | None:
    """The commit `ref` names, or None when it names nothing."""
    out = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() or None


def delete_branch(repo: Path, branch: str) -> None:
    """Force-delete `branch`; a no-op when it does not exist, and a git failure raises."""
    if rev_parse(repo, f"refs/heads/{branch}") is None:
        return
    _git(repo, "branch", "-D", branch)


def squash_since(worktree: Path, base_sha: str, message: str) -> str:
    """Everything since `base_sha`, committed or not, as one commit by airlie; returns its sha. A stacked PR
    then shows one reviewable commit instead of the studio's WIP rounds."""
    _git(worktree, *_ENV, "add", "-A")
    _git(worktree, "reset", "-q", "--soft", base_sha)
    _git(worktree, *_ENV, "commit", "-q", "-m", message)
    return head_sha(worktree)


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


def head_sha(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def diff_between(worktree: Path, base_sha: str, max_chars: int) -> str:
    """The diff from `base_sha` to HEAD, capped."""
    out = subprocess.run(
        ["git", "diff", "--no-color", base_sha, "HEAD"], cwd=worktree, capture_output=True, text=True
    ).stdout
    return out[:max_chars]


def changed_files(worktree: Path, base_sha: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", base_sha, "HEAD"], cwd=worktree, capture_output=True, text=True
    ).stdout
    return [line for line in out.splitlines() if line]


def remove_worktree(repo: Path, path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)], cwd=repo, capture_output=True, text=True
    )
    subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True, text=True)
