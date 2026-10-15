"""One git worktree per team/branch under the run directory, each on its own git branch; fork helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from airlie.subproc import NO_HOOKS_ARGS, agent_env


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """airlie's own git. It runs no hook and no fsmonitor (NO_HOOKS: a model can plant either in the
    repository it works in) and gets the agent environment, since it needs no credential, plus `env`."""
    return subprocess.run(
        ["git", *NO_HOOKS_ARGS, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
        env={**agent_env(), **(env or {})},
        input=stdin,
    )


AIRLIE = ("airlie", "airlie@local")
"""Who commits in a worktree: the studio's WIP rounds, and a layer's commit unless a GitHub App's bot does."""
_ENV = ["-c", f"user.name={AIRLIE[0]}", "-c", f"user.email={AIRLIE[1]}"]


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
    out = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out.stdout.strip() or None


def delete_branch(repo: Path, branch: str) -> None:
    """Force-delete `branch`; a no-op when it does not exist, and a git failure raises."""
    if rev_parse(repo, f"refs/heads/{branch}") is None:
        return
    _git(repo, "branch", "-D", branch)


def squash_since(
    worktree: Path, base_sha: str, message: str, *, author: tuple[str, str] | None = None
) -> str:
    """Everything since `base_sha`, committed or not, as one commit; returns its sha. A stacked PR then shows
    one reviewable commit instead of the studio's WIP rounds. `author` (name, email), airlie by default (a
    GitHub App's bot when the story publishes as one), is its author and its committer, whatever
    GIT_AUTHOR_* or GIT_COMMITTER_* in the environment say: they would win over user.name and user.email."""
    name, email = author or AIRLIE
    who = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email}
    who |= {"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    _git(worktree, *_ENV, "add", "-A")
    _git(worktree, "reset", "-q", "--soft", base_sha)
    identity = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    _git(worktree, *identity, "commit", "-q", "-m", message, env=who)
    return head_sha(worktree)


def commit_wip(worktree: Path, message: str) -> str:
    """Commit everything in the worktree (untracked included; empty allowed) and return the new sha."""
    _git(worktree, *_ENV, "add", "-A")
    _git(worktree, *_ENV, "commit", "-q", "--allow-empty", "-m", message)
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def diff_last_commit(worktree: Path, max_chars: int) -> str:
    """The last commit's diff (what the branch changed since the previous fork base), capped."""
    return _git(worktree, "show", "--format=", "--no-color", "HEAD", check=False).stdout[:max_chars]


def head_sha(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def diff_between(worktree: Path, base_sha: str, max_chars: int) -> str:
    """The diff from `base_sha` to HEAD, capped."""
    return _git(worktree, "diff", "--no-color", base_sha, "HEAD", check=False).stdout[:max_chars]


def changed_files(worktree: Path, base_sha: str) -> list[str]:
    out = _git(worktree, "diff", "--name-only", base_sha, "HEAD", check=False).stdout
    return [line for line in out.splitlines() if line]


def show_file(repo: Path, commit: str, path: str) -> tuple[str, str] | None:
    """The file at `path` in `commit`, from git's objects (never a worktree): its blob's sha and its text,
    read as UTF-8. A symbolic link to another file of the commit is followed; None when the commit has no
    file there (nothing, a directory, or a link that leaves the commit or leads nowhere)."""
    found = _git(
        repo, "cat-file", "--batch-check", "--follow-symlinks", stdin=f"{commit}:{path}\n", check=False
    ).stdout.split()
    if len(found) != 3 or found[1] != "blob":  # "<sha> blob <size>"; else missing, a tree, a link out
        return None
    data = subprocess.run(
        ["git", *NO_HOOKS_ARGS, "cat-file", "blob", found[0]],
        cwd=repo,
        capture_output=True,
        check=True,
        env=agent_env(),
    ).stdout
    return found[0], data.decode("utf-8", errors="replace")


def remote_url(repo: Path, remote: str) -> str | None:
    """The first URL `remote` is configured with (remote.<name>.url as written: no insteadOf applied), or None
    when it has none."""
    urls = _git(repo, "config", "--get-all", f"remote.{remote}.url", check=False).stdout.splitlines()
    return urls[0] if urls else None


def remove_worktree(repo: Path, path: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(path), check=False)
    _git(repo, "worktree", "prune", check=False)
