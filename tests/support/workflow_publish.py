"""Shared helpers extracted from tests/unit/test_workflow_publish.py for tests.support.workflow_publish."""

from __future__ import annotations

from pathlib import Path

from tests.support.git import git


def home_with_origin(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[Path, Path]:
    """airlie's checkout as serve runs it (`home`, on main) and the bare repository its origin names."""
    origin, home = tmp_path / "origin.git", tmp_path / "home"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(home))
    for path, text in {"README.md": "airlie\n", **(files or {})}.items():
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text)
    git(home, "add", ".")
    git(home, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    git(home, "remote", "add", "origin", str(origin))
    git(home, "push", "-q", "origin", "main")
    return home, origin
