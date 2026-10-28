"""One git worktree per team/branch under the run directory, each on its own git branch; fork helpers."""

from __future__ import annotations

import re
import shutil
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


def seed_mypy_cache(repo: Path, worktree: Path) -> None:
    """`worktree/.mypy_cache` as a copy of `repo`'s, when `repo` has one: a fresh worktree then runs mypy
    warm instead of cold (0.7 s against 18.8 s in this repository, performance review of 2026-10-23,
    finding 4; mypy re-hashes a module whose path or mtime differ from its cache entry and keeps the entry
    when the hash matches). A copy, not one directory shared through MYPY_CACHE_DIR: layers and stories
    will run in parallel, and several mypy runs writing one cache at once is a race. A copy that fails
    part-way is removed again, since mypy may trip over half a cache where a missing one only costs the
    cold run. mypy writes a `.gitignore` into the cache, so no commit of the worktree picks the copy up."""
    source = repo / ".mypy_cache"
    if not source.is_dir():
        return
    target = worktree / ".mypy_cache"
    try:
        shutil.copytree(source, target)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)


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


_TRAILER_KEY = re.compile(r"[A-Za-z-]+")
_TRAILER_VALUE_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def _trailers(trailers: Mapping[str, str]) -> str:
    """`trailers` as git trailer lines, in order. A key is letters and hyphens; a value is one line with no
    control character or line separator, so a value can never start a trailer or a paragraph of its own. A
    ValueError names the trailer, before any git call: a commit message is where provenance must not be
    forged."""
    lines = []
    for key, value in trailers.items():
        if not _TRAILER_KEY.fullmatch(key):
            raise ValueError(f"trailer key {key!r} must be letters and hyphens")
        if _TRAILER_VALUE_BAD.search(value):
            raise ValueError(f"trailer {key} must be one line with no control characters")
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def squash_since(
    worktree: Path,
    base_sha: str,
    message: str,
    *,
    author: tuple[str, str] | None = None,
    trailers: Mapping[str, str] | None = None,
) -> str:
    """Everything since `base_sha`, committed or not, as one commit; returns its sha. A stacked PR then shows
    one reviewable commit instead of the studio's WIP rounds. `author` (name, email), airlie by default (a
    GitHub App's bot when the story publishes as one), is its author and its committer, whatever
    GIT_AUTHOR_* or GIT_COMMITTER_* in the environment say: they would win over user.name and user.email.
    `trailers` (P6, 2026-10-25) end the message after a blank line, one `Key: value` line each in the order
    given, for `git interpret-trailers` to read: keys, numbers and shas, never model text (`_trailers`)."""
    if trailers:
        message = f"{message}\n\n{_trailers(trailers)}"
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


FRESH_DIFF_CHARS = 12_000  # about 3k tokens: what a fresh start may carry of the role's own diff


def own_changes(worktree: Path, since: str, limit: int = FRESH_DIFF_CHARS) -> str:
    """What the worktree holds now that `since` did not, as one fenced diff block (untracked files shown as
    new files; the index is left alone). Over `limit` characters it is one line per file, `path (+added
    -removed)`, instead. "" when nothing changed. AIR-9 layer 05, 2026-10-23: a role started fresh with only
    "your changes are in the worktree" spent its 20 steps re-reading them; this hands it the diff."""
    tracked = _git(worktree, "diff", "--no-color", since, check=False).stdout
    new = [
        f
        for f in _git(worktree, "ls-files", "--others", "--exclude-standard", check=False).stdout.splitlines()
        if f
    ]
    parts = [tracked] if tracked else []
    for f in new:
        # --no-index exits 1 when the files differ, which they do
        parts.append(
            _git(worktree, "diff", "--no-color", "--no-index", "--", "/dev/null", f, check=False).stdout
        )
    diff = "".join(parts).rstrip("\n")
    if not diff:
        return ""
    if len(diff) <= limit:
        return f"```diff\n{diff}\n```"
    counts: dict[str, tuple[int, int]] = {}
    for line in _git(worktree, "diff", "--numstat", since, check=False).stdout.splitlines():
        added, removed, path = line.split("\t", 2)  # "-" for a binary file
        counts[path] = (int(added) if added.isdigit() else 0, int(removed) if removed.isdigit() else 0)
    for f in new:
        try:
            lines = len((worktree / f).read_bytes().splitlines())
        except OSError:
            lines = 0
        counts[f] = (lines, 0)
    listing = "\n".join(f"{path} (+{a} \u2212{r})" for path, (a, r) in counts.items())
    return f"Too long to show; the files you changed:\n{listing}"


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


def tree_paths(repo: Path, commit: str, prefix: str = "") -> list[str]:
    """Every file of `commit` under `prefix` ("" for all), from git's objects (never a worktree), as paths
    from the repository's root, in git's order; [] when `prefix` is not there."""
    spec = ["--", prefix] if prefix else []
    out = _git(repo, "ls-tree", "-r", "-z", "--name-only", commit, *spec, check=False).stdout
    return [p for p in out.split("\0") if p]


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
