"""Shared helpers extracted from tests/unit/test_story_restack.py for tests.support.story_restack."""

from __future__ import annotations

from pathlib import Path

from tests.support.git import git


def trunk_commit(repo: Path, files: dict[str, str], message: str = "trunk moves on") -> str:
    """A commit on the repository's trunk (checked out in `repo`): the mirrored fetch then finds it there."""
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")
