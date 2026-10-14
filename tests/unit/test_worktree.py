"""Worktree helpers used by the studio: diff and changed files since a base commit."""

import subprocess
from pathlib import Path

from tests.unit.test_fork_orchestrator import repo_with_marker_tests


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
