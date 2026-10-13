"""Deterministic repository map injected before the goal (design §4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from airlie.repo_map import build_repo_map


def git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *env, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *env, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


FILES = {
    "pkg/__init__.py": "",
    "pkg/report.py": (
        "class Report:\n    pass\n\n\ndef total_cost(qty: int, unit_cost_cents: int) -> int:\n    return 0\n"
    ),
    "pkg/broken.py": "def oops(:\n",
    "tests/test_report.py": "def test_total():\n    pass\n",
    "docs/a/b/c/deep.md": "x",
    "Makefile": "all:\n",
}


def test_map_lists_tree_signatures_and_test_command(tmp_path: Path) -> None:
    text, cached = build_repo_map(
        git_repo(tmp_path, FILES), test_cmd="pytest -q", max_chars=6000, depth=3, cache_dir=None
    )
    assert cached is False
    assert text.startswith("[Repository map]\ntest command: pytest -q\n")
    assert "pkg/report.py" in text and "def total_cost(qty: int, unit_cost_cents: int) -> int" in text
    assert "class Report" in text
    assert "pkg/broken.py" in text  # listed, signatures skipped
    assert "Makefile" in text
    assert "docs/a/b/c/deep.md" not in text and "docs/a/b/" in text  # depth 3 cut


def test_map_is_capped_and_deterministic(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, FILES)
    a, _ = build_repo_map(repo, test_cmd=None, max_chars=120, depth=3, cache_dir=None)
    b, _ = build_repo_map(repo, test_cmd=None, max_chars=120, depth=3, cache_dir=None)
    assert a == b and len(a) <= 120 + len("\n[map truncated]") and a.endswith("[map truncated]")


def test_map_cache_hits_on_second_call_and_misses_after_change(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, FILES)
    cache = tmp_path / "cache"
    _, c1 = build_repo_map(repo, test_cmd=None, max_chars=6000, depth=3, cache_dir=cache)
    _, c2 = build_repo_map(repo, test_cmd=None, max_chars=6000, depth=3, cache_dir=cache)
    assert (c1, c2) == (False, True)
    (repo / "pkg" / "new.py").write_text("def added(): ...\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    text, c3 = build_repo_map(repo, test_cmd=None, max_chars=6000, depth=3, cache_dir=cache)
    assert c3 is False and "def added()" in text
