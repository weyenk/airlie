"""Shared test helpers for tests.support.story_layer_checks."""

from __future__ import annotations

from pathlib import Path

from tests.support.git import git

CALC = "def add(a, b):\n    return a + b\n"

OLD_TESTS = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    """A git repository holding `files` (and a pytest.ini, so the rootdir is the repository) in one commit;
    returns it and that commit."""
    root = tmp_path / "repo"
    root.mkdir()
    for path, text in {"pytest.ini": "[pytest]\n", **files}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "init", "-q")
    return root, commit(root, "base")


def commit(root: Path, message: str, files: dict[str, str] | None = None) -> str:
    for path, text in (files or {}).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")
