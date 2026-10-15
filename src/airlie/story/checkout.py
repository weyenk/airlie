"""The repository a Jira story is built in. The issue names it with one label, repo:<owner>/<name>, which a
model cannot move: agents get no Jira credentials, and airlie only comments and transitions. Its checkout is
the directory under story.repos_root named <name>, in any case, and that checkout's remote, as written, must
name the same repository: a story is built only in a clone of the repository its issue names. Each problem is
a RepositoryError whose message is for the issue."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from airlie.story.github_app import GitHubAppError, masked, repo_slug
from airlie.story.model import RepositoryError
from airlie.teams.worktree import remote_url

PREFIX = "repo:"
"""A label that starts with this, in any case, names the issue's repository."""
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", re.ASCII)
"""owner/name, each of GitHub's characters."""
_QUOTED = 80
"""How many characters of a label a message quotes: labels are issue text."""
_EXAMPLE = "repo:weyenk/OpenHand"


def _quoted(label: str) -> str:
    """`label` as a message quotes it: its repr (a control character shows as its escape), cut to _QUOTED."""
    text = repr(label)
    return text if len(text) <= _QUOTED else text[: _QUOTED - 1] + "…"


def repo_labels(labels: Iterable[str]) -> list[str]:
    """The labels that name a repository: those that start with repo:, in any case."""
    return [x for x in labels if x[: len(PREFIX)].lower() == PREFIX]


def repo_label(labels: Iterable[str]) -> str:
    """The "owner/name" of the issue's one repo:<owner>/<name> label, as the label spells it (owner and name
    are letters, digits, ".", "_" and "-"); a RepositoryError when it has none, more than one, or a malformed
    one."""
    found = repo_labels(labels)
    if not found:
        raise RepositoryError(
            f"the issue names no repository: add the label repo:<owner>/<name> ({_EXAMPLE}, say), the "
            "GitHub repository it is for"
        )
    if len(found) > 1:
        raise RepositoryError(
            f"the issue has {len(found)} repo: labels ({', '.join(map(_quoted, found))}); keep the one that "
            "names the repository it is for"
        )
    named = found[0][len(PREFIX) :]
    if not _REPOSITORY.fullmatch(named) or {".", ".."} & set(named.split("/")):
        raise RepositoryError(
            f"the label {_quoted(found[0])} is not repo:<owner>/<name> (each of letters, digits, '.', '_' "
            f"and '-'; {_EXAMPLE}, say)"
        )
    return named


def held_label(labels: Iterable[str], pinned: str | None, state: Path) -> str:
    """repo_label, for an issue whose story was started for `pinned` ("owner/name"; None when no story was,
    or none is on record) and keeps its state in `state`: a label that no longer names that repository (in any
    case), or no longer one, is a RepositoryError that says from what to what."""
    labels = list(labels)
    try:
        label = repo_label(labels)
    except RepositoryError:
        if pinned is None:
            raise
        now = ", ".join(map(_quoted, repo_labels(labels))) or "none"
    else:
        if pinned is None or label.lower() == pinned.lower():
            return label
        now = f"repo:{label}"
    raise RepositoryError(  # outside the handler: nothing is chained
        f"the issue's repo: label changed after its story started, from repo:{pinned} to {now}: put "
        f"repo:{pinned} back, or delete {state} to start the story over"
    )


def find_checkout(root: Path, github_repo: str) -> Path:
    """The directory in `root` (story.repos_root) named as `github_repo` ("owner/name") is, in any case; a
    RepositoryError that names `root` when there is none (and says to clone the repository there) or more than
    one (a volume where names are case-sensitive)."""
    name = github_repo.split("/", 1)[1]
    try:
        found = sorted(p for p in root.iterdir() if p.name.lower() == name.lower() and p.is_dir())
    except OSError:  # no such directory, not a directory, not readable: no checkout is there
        found = []
    if not found:
        raise RepositoryError(
            f"no checkout of {github_repo} in {root} (story.repos_root): clone it there: "
            f"git clone git@github.com:{github_repo}.git {root / name}"
        )
    if len(found) > 1:
        raise RepositoryError(
            f"more than one directory in {root} (story.repos_root) is named {name} in some case: "
            f"{', '.join(map(str, found))}; keep the one that is the checkout of {github_repo}"
        )
    return found[0]


def check_checkout(path: Path, remote: str, github_repo: str) -> None:
    """`path` must be a git repository whose `remote` (story.remote), as written (no insteadOf applied: the
    first URL, which every fetch uses), names `github_repo` in one of GitHub's forms, in any case; a
    RepositoryError says what it is instead, and what it should be (a password in the URL masked)."""
    ssh = f"git@github.com:{github_repo}.git"
    if not (path / ".git").exists():
        raise RepositoryError(f"{path} is not a git repository: clone {github_repo} there (git clone {ssh})")
    url = remote_url(path, remote)
    if url is None:
        add = f"git -C {path} remote add {remote} {ssh}"
        raise RepositoryError(f"{path} has no remote '{remote}' (story.remote): add it with {add}")
    try:
        named: str | None = repo_slug(url)
    except GitHubAppError:  # not a github.com repository at all
        named = None
    if named is None or named.lower() != github_repo.lower():
        raise RepositoryError(
            f"{path} is not a checkout of {github_repo}: its remote '{remote}' (story.remote) is "
            f"{masked(url)!r}, and the issue's label wants {ssh} (or ssh://git@github.com/{github_repo}.git, "
            f"or https://github.com/{github_repo}.git)"
        )
