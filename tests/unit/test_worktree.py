"""Worktree helpers used by the studio: diff and changed files since a base commit; airlie's own git runs no
hook and no fsmonitor."""

import shlex
import subprocess
from pathlib import Path

import pytest

from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests


def script(path: Path, mark: Path, code: int = 0) -> None:
    """An executable at `path` that leaves `mark` when it runs, then exits `code`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(mark))}\nexit {code}\n")
    path.chmod(0o755)


def plant(repo: Path, wt: Path, marks: Path, hooks: str) -> None:
    """What a model's shell can plant from a worktree `wt` of `repo`: a pre-commit hook (in the repository's
    hooks directory, or under a relative core.hooksPath inside the worktree) and an fsmonitor; each leaves a
    file in `marks` when it runs."""
    if hooks == "hooks directory":
        script(repo / ".git" / "hooks" / "pre-commit", marks / "pre-commit")
    else:
        git(wt, "config", "core.hooksPath", ".hooks")
        script(wt / ".hooks" / "pre-commit", marks / "pre-commit")
    git(wt, "config", "core.fsmonitor", "./.fsmonitor.sh")
    script(wt / ".fsmonitor.sh", marks / "fsmonitor", code=1)  # a failed query: git looks at every file


@pytest.mark.parametrize("hooks", ["hooks directory", "relative core.hooksPath"])
def test_airlies_commits_run_no_hook_and_no_fsmonitor_a_model_planted(tmp_path: Path, hooks: str) -> None:
    from airlie.teams.worktree import add_worktree, changed_files, commit_wip, head_sha, squash_since

    repo = repo_with_marker_tests(tmp_path)
    base = head_sha(repo)
    wt = add_worktree(repo, tmp_path / "wt", branch="layer-01", start_point=base)
    marks = tmp_path / "marks"
    marks.mkdir()
    plant(repo, wt, marks, hooks)
    (wt / "a.txt").write_text("a\n")
    commit_wip(wt, "wip")
    (wt / "b.txt").write_text("b\n")
    squash_since(wt, base, "story: layer one")
    assert sorted(p.name for p in marks.iterdir()) == []
    assert {"a.txt", "b.txt"} <= set(changed_files(wt, base))  # and the commits hold the work
    # the control: git without airlie's settings runs both, so they were planted where git looks
    (wt / "c.txt").write_text("c\n")
    git(wt, "add", "-A")
    git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "plain")
    assert sorted(p.name for p in marks.iterdir()) == ["fsmonitor", "pre-commit"]


def test_diff_between_and_changed_files(tmp_path: Path) -> None:
    from airlie.teams.worktree import changed_files, commit_wip, diff_between

    repo = repo_with_marker_tests(tmp_path)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
    base = head.stdout.strip()
    (repo / "new.py").write_text("x = 1\n")
    commit_wip(repo, "wip")
    assert changed_files(repo, base) == ["new.py"]
    assert "+x = 1" in diff_between(repo, base, max_chars=1000)
    assert diff_between(repo, base, max_chars=5) == "diff "


def test_head_sha(tmp_path: Path) -> None:
    from airlie.teams.worktree import commit_wip, head_sha

    repo = repo_with_marker_tests(tmp_path)
    before = head_sha(repo)
    (repo / "x").write_text("x")
    assert commit_wip(repo, "wip") == head_sha(repo) != before


def _branches(repo: Path) -> list[str]:
    fmt = "--format=%(refname:short)"
    out = subprocess.run(["git", "branch", fmt], cwd=repo, capture_output=True, text=True)
    return sorted(out.stdout.split())


def _current(wt: Path) -> str:
    out = subprocess.run(["git", "branch", "--show-current"], cwd=wt, capture_output=True, text=True)
    return out.stdout.strip()


def test_a_detached_worktree_leaves_no_branch(tmp_path: Path) -> None:
    from airlie.teams.worktree import add_worktree

    repo = repo_with_marker_tests(tmp_path)
    before = _branches(repo)
    wt = add_worktree(repo, tmp_path / "wt", branch=None)
    assert (wt / "README.md").exists() and _current(wt) == "" and _branches(repo) == before


def test_squash_since_folds_commits_and_uncommitted_work_into_one(tmp_path: Path) -> None:
    from airlie.teams.worktree import add_worktree, changed_files, commit_wip, head_sha, squash_since

    repo = repo_with_marker_tests(tmp_path)
    base = head_sha(repo)
    wt = add_worktree(repo, tmp_path / "wt", branch="layer-01", start_point=base)
    (wt / "a.txt").write_text("a\n")
    commit_wip(wt, "wip 1")
    (wt / "b.txt").write_text("b\n")  # never committed
    sha = squash_since(wt, base, "story: layer one")
    log = subprocess.run(
        ["git", "log", "--format=%s|%an", f"{base}..{sha}"], cwd=wt, capture_output=True, text=True
    )
    assert log.stdout.splitlines() == ["story: layer one|airlie"]
    assert changed_files(wt, base) == ["a.txt", "b.txt"]


def test_rev_parse_checkout_worktree_and_delete_branch(tmp_path: Path) -> None:
    from airlie.teams.worktree import (
        add_worktree,
        checkout_worktree,
        delete_branch,
        head_sha,
        remove_worktree,
        rev_parse,
    )

    repo = repo_with_marker_tests(tmp_path)
    assert rev_parse(repo, "nope") is None and rev_parse(repo, "HEAD") == head_sha(repo)
    remove_worktree(repo, add_worktree(repo, tmp_path / "wt", branch="top"))
    pub = checkout_worktree(repo, tmp_path / "pub", "top")
    assert _current(pub) == "top"
    remove_worktree(repo, pub)
    delete_branch(repo, "top")
    assert rev_parse(repo, "top") is None


def test_delete_branch_of_a_branch_that_never_existed_does_not_raise(tmp_path: Path) -> None:
    from airlie.teams.worktree import delete_branch

    repo = repo_with_marker_tests(tmp_path)
    before = _branches(repo)
    delete_branch(repo, "never-existed")
    assert _branches(repo) == before


def test_delete_branch_raises_when_git_refuses(tmp_path: Path) -> None:
    from airlie.teams.worktree import add_worktree, delete_branch

    repo = repo_with_marker_tests(tmp_path)
    add_worktree(repo, tmp_path / "wt", branch="busy")
    with pytest.raises(subprocess.CalledProcessError):
        delete_branch(repo, "busy")


@pytest.mark.parametrize("author", [None, ("app-bot[bot]", "41+app-bot[bot]@users.noreply.github.com")])
def test_the_squash_is_authored_and_committed_by_who_it_names_whatever_the_environment_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, author: tuple[str, str] | None
) -> None:
    """airlie by default; a GitHub App's bot when the story publishes as one. GIT_AUTHOR_* and
    GIT_COMMITTER_* would win over user.name and user.email (ledger L64)."""
    from airlie.teams.worktree import add_worktree, head_sha, squash_since

    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Someone Else")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "someone@else.example")
    repo = repo_with_marker_tests(tmp_path)
    base = head_sha(repo)
    wt = add_worktree(repo, tmp_path / "wt", branch="layer-01", start_point=base)
    (wt / "a.txt").write_text("a\n")
    sha = squash_since(wt, base, "story: layer one", author=author)
    name, email = author or ("airlie", "airlie@local")
    assert git(wt, "log", "-1", "--format=%an|%ae|%cn|%ce", sha) == f"{name}|{email}|{name}|{email}"
    assert git(wt, "log", "-1", "--format=%s", sha) == "story: layer one"


def test_remote_url_is_the_first_url_as_written(tmp_path: Path) -> None:
    from airlie.teams.worktree import remote_url

    repo = repo_with_marker_tests(tmp_path)
    assert remote_url(repo, "origin") is None
    git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    git(repo, "config", "url.https://github.com/elsewhere/.insteadOf", "git@github.com:o/")  # not applied
    git(repo, "remote", "set-url", "--add", "origin", "git@github.com:evil/other.git")  # a second: not read
    assert remote_url(repo, "origin") == "git@github.com:o/r.git"
    assert remote_url(repo, "upstream") is None


def test_a_read_only_worktree_moves_to_another_commit_detached(tmp_path: Path) -> None:
    """P9 (2026-10-29): the architect's drafting conversation, held across a story's layers, reads each later
    layer's real base: its checkout moves there before the turn."""
    from airlie.teams.worktree import add_worktree, detach_at, head_sha

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    first = git(repo, "rev-parse", "HEAD").strip()
    (repo / "b.py").write_text("y = 2\n")
    git(repo, "add", "b.py")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "two")
    second = git(repo, "rev-parse", "HEAD").strip()
    tree = tmp_path / "talk"
    add_worktree(repo, tree, branch=None, start_point=first)
    assert not (tree / "b.py").exists()
    detach_at(tree, second)
    assert head_sha(tree) == second and (tree / "b.py").read_text() == "y = 2\n"
