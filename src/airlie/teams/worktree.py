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


def dirty(worktree: Path) -> bool:
    """Whether the worktree has changes not in HEAD (modified, added or untracked files)."""
    return bool(_git(worktree, "status", "--porcelain", "--untracked-files=all", check=False).stdout.strip())


def head_sha(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def diff_between(worktree: Path, base_sha: str, max_chars: int) -> str:
    """The diff from `base_sha` to HEAD, capped."""
    return _git(worktree, "diff", "--no-color", base_sha, "HEAD", check=False).stdout[:max_chars]


def diff_commits(repo: Path, base_sha: str, head_sha: str, max_chars: int) -> str:
    """The diff from `base_sha` to `head_sha` in `repo`, capped."""
    return _git(repo, "diff", "--no-color", base_sha, head_sha, check=False).stdout[:max_chars]


def changed_in_tree(worktree: Path, base_sha: str) -> list[str]:
    """The files that differ between `base_sha` and the working tree, committed or not, untracked included:
    what the run and the subtasks before it have written so far."""
    diff = _git(worktree, "diff", "--name-only", base_sha, check=False).stdout
    new = _git(worktree, "ls-files", "--others", "--exclude-standard", check=False).stdout
    seen: dict[str, None] = {}
    for line in (*diff.splitlines(), *new.splitlines()):
        if line:
            seen.setdefault(line, None)
    return list(seen)


def changed_files(worktree: Path, base_sha: str) -> list[str]:
    out = _git(worktree, "diff", "--name-only", base_sha, "HEAD", check=False).stdout
    return [line for line in out.splitlines() if line]


def put_back(worktree: Path, commit: str, paths: list[str]) -> None:
    """`paths` in the worktree as they are in `commit`: checked out from it, or removed where it has none.
    `reset_to_head` undoes it."""
    if not paths:
        return
    there = _git(worktree, "ls-tree", "-z", "--name-only", commit, "--", *paths).stdout.split("\0")
    kept = [p for p in paths if p in there]
    if kept:
        _git(worktree, "checkout", "-q", commit, "--", *kept)
    for p in paths:
        if p not in kept:
            (worktree / p).unlink(missing_ok=True)


def differs_from(worktree: Path, commit: str, paths: list[str]) -> list[str]:
    """Which of `paths` the worktree's files (committed or not) have changed or removed since `commit`."""
    if not paths:
        return []
    out = _git(worktree, "diff", "--name-only", commit, "--", *paths, check=False).stdout
    return [line for line in out.splitlines() if line]


def reset_to_head(worktree: Path) -> None:
    """The worktree as its HEAD commit holds it: changes undone and untracked files removed (ignored ones, a
    virtualenv say, are kept)."""
    _git(worktree, "reset", "-q", "--hard", "HEAD")
    _git(worktree, "clean", "-q", "-fd")


def find_file(repo: Path, commit: str, path: str) -> tuple[str, str]:
    """What `path` is in `commit`, from git's objects (never a worktree), a symbolic link to another file of
    the commit followed: ("blob", its sha) for a file; ("symlink", where it points) for a link out of the
    commit; ("dangling" | "loop" | "notdir", "") for a link that leads to no file of it; ("tree" | "commit",
    its sha) for a directory or a submodule; ("missing", "") when there is nothing there."""
    out = _git(
        repo, "cat-file", "--batch-check", "--follow-symlinks", stdin=f"{commit}:{path}\n", check=False
    ).stdout
    first, _, rest = out.partition("\n")
    words = first.split()
    if words[:1] == ["symlink"]:  # "symlink <size>", then the link's target on a line of its own
        return "symlink", rest.removesuffix("\n")
    if words[:1] in (["dangling"], ["loop"], ["notdir"]):
        return words[0], ""
    if len(words) == 3:  # "<sha> <type> <size>"
        return words[1], words[0]
    return "missing", ""


def read_blob(repo: Path, sha: str) -> str:
    """The blob `sha` as text, read as UTF-8 (anything else replaced)."""
    data = subprocess.run(
        ["git", *NO_HOOKS_ARGS, "cat-file", "blob", sha],
        cwd=repo,
        capture_output=True,
        check=True,
        env=agent_env(),
    ).stdout
    return data.decode("utf-8", errors="replace")


def remote_url(repo: Path, remote: str) -> str | None:
    """The first URL `remote` is configured with (remote.<name>.url as written: no insteadOf applied), or None
    when it has none."""
    urls = _git(repo, "config", "--get-all", f"remote.{remote}.url", check=False).stdout.splitlines()
    return urls[0] if urls else None


def remove_worktree(repo: Path, path: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(path), check=False)
    _git(repo, "worktree", "prune", check=False)


def cherry_pick(worktree: Path, commit: str) -> list[str] | None:
    """`commit` replayed onto the worktree's HEAD, with its author as author and committer (a layer the bot
    committed stays the bot's). None when it applied; else the paths that conflicted (or git's message), and
    the pick is aborted, so the worktree is as it was."""
    name, email = _git(worktree, "log", "-1", "--format=%an%n%ae", commit).stdout.splitlines()[:2]
    who = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email}
    who |= {"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    picked = _git(worktree, "cherry-pick", "--allow-empty", commit, check=False, env=who)
    if picked.returncode == 0:
        return None
    clashes = _git(worktree, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    _git(worktree, "cherry-pick", "--abort", check=False)
    return clashes or [(picked.stderr.strip() or picked.stdout.strip())[-500:]]


def move_branches(repo: Path, moves: Mapping[str, tuple[str, str]]) -> None:
    """Each branch in `moves` from its old commit to its new one ({branch: (old, new)}), in one transaction:
    all move or, when any is not at its old commit any more, none does (a CalledProcessError)."""
    lines = [f"update refs/heads/{b} {new} {old}" for b, (old, new) in moves.items()]
    _git(repo, "update-ref", "--stdin", stdin="start\n" + "\n".join(lines) + "\nprepare\ncommit\n")
