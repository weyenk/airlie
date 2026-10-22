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


def test_a_tracked_path_the_checkout_cannot_stat_or_read_does_not_stop_the_map(tmp_path: Path) -> None:
    """Every studio run maps its fresh worktree, where a tracked symlink to an ignored file dangles (the file
    is not in it). Such a path is left out; a file nobody may read is listed without signatures."""
    from airlie.teams.worktree import add_worktree

    repo = git_repo(tmp_path, FILES)
    (repo / ".gitignore").write_text("local.cfg\n")
    (repo / "local.cfg").write_text("only in the person's checkout\n")
    (repo / "config.cfg").symlink_to("local.cfg")  # tracked; its target is ignored
    (repo / "gone.py").symlink_to("missing.py")  # tracked, and dangling everywhere
    (repo / "pkg" / "locked.py").write_text("def hidden() -> None: ...\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "links"], cwd=repo, check=True
    )
    wt = add_worktree(repo, tmp_path / "wt", branch=None)
    locked = wt / "pkg" / "locked.py"
    locked.chmod(0)
    try:
        text, cached = build_repo_map(
            wt, test_cmd=None, max_chars=6000, depth=3, cache_dir=tmp_path / "cache"
        )
    finally:
        locked.chmod(0o644)
    assert cached is False and "config.cfg" not in text and "gone.py" not in text  # nothing to stat: left out
    assert "pkg/report.py" in text and "def total_cost(" in text and ".gitignore" in text  # the rest is there
    assert "pkg/locked.py (" in text and "hidden" not in text  # listed; its signatures could not be read


def _focus_repo(tmp_path: Path) -> Path:
    return git_repo(
        tmp_path,
        {
            "src/pkg/a.py": "def alpha() -> int:\n    return 1\n",
            "tests/test_a.py": "def test_a():\n    pass\n",
            "README.md": "hi\n",
            **{f"docs/page{i:03}.md": "x" * 2000 for i in range(200)},
        },
    )


def _lines(text: str) -> list[str]:
    return text.splitlines()


def test_code_and_tests_come_before_docs_that_outweigh_the_budget(tmp_path: Path) -> None:
    text, _ = build_repo_map(_focus_repo(tmp_path), test_cmd=None, max_chars=3000, depth=3, cache_dir=None)
    assert "src/pkg/a.py" in text and "tests/test_a.py" in text
    assert "docs/ (200 files)" in _lines(text)


def test_focus_files_come_first_with_their_outline(tmp_path: Path) -> None:
    text, _ = build_repo_map(
        _focus_repo(tmp_path),
        test_cmd="task test",
        max_chars=3000,
        depth=3,
        cache_dir=None,
        focus=["src/pkg/a.py"],
    )
    assert "## In scope" in text
    scope = text.index("## In scope")
    assert scope < text.index("docs/ (") and scope < text.index("src/ (")
    assert "1: def alpha" in text[scope : text.index("src/ (")]


def test_an_untracked_focus_path_is_left_out(tmp_path: Path) -> None:
    text, _ = build_repo_map(
        _focus_repo(tmp_path),
        test_cmd=None,
        max_chars=3000,
        depth=3,
        cache_dir=None,
        focus=["src/pkg/new.py"],
    )
    assert "new.py" not in text and "## In scope" not in text
    assert any(line.startswith("src/ (") for line in _lines(text))
    assert any(line.startswith("tests/ (") for line in _lines(text))


def test_every_top_level_directory_is_named_even_when_truncated(tmp_path: Path) -> None:
    text, _ = build_repo_map(_focus_repo(tmp_path), test_cmd=None, max_chars=400, depth=3, cache_dir=None)
    for d in ("docs", "src", "tests"):
        assert any(line.startswith(f"{d}/ (") for line in _lines(text))
    assert text.endswith("[map truncated]")


def test_focus_is_part_of_the_cache_key(tmp_path: Path) -> None:
    repo, cache = _focus_repo(tmp_path), tmp_path / "cache"
    plain, _ = build_repo_map(repo, test_cmd=None, max_chars=3000, depth=3, cache_dir=cache)
    focused, cached = build_repo_map(
        repo, test_cmd=None, max_chars=3000, depth=3, cache_dir=cache, focus=["src/pkg/a.py"]
    )
    assert cached is False and "## In scope" in focused and "## In scope" not in plain


def test_a_directory_too_large_to_expand_is_expanded_one_level(tmp_path: Path) -> None:
    files = {f"big/sub{i % 30}/f{i}.py": "def f() -> None: ...\n" for i in range(300)}
    files["big/top.py"] = "def top() -> None: ...\n"
    files["README.md"] = "x\n"
    text, _ = build_repo_map(
        git_repo(tmp_path, files), test_cmd=None, max_chars=3000, depth=3, cache_dir=None
    )
    assert any(line.startswith("big/sub0/ (") for line in _lines(text))
    assert any(line.startswith("big/top.py (") for line in _lines(text))
    assert text.endswith("[map truncated]")


def test_code_directories_with_more_code_come_first(tmp_path: Path) -> None:
    files = {f"src/pkg/m{i}.py": "x = 1\n" for i in range(10)}
    files.update({f"scripts/s{i}.py": "x = 1\n" for i in range(5)})
    text, _ = build_repo_map(
        git_repo(tmp_path, files), test_cmd=None, max_chars=6000, depth=3, cache_dir=None
    )
    assert text.index("src/pkg/m0.py") < text.index("scripts/s0.py")


def test_large_focus_files_never_push_out_the_top_level_directories(tmp_path: Path) -> None:
    big = "".join(
        f"def function_number_{i}(argument: int) -> int:\n    return argument\n\n" for i in range(80)
    )
    files = {f"src/pkg/big{i}.py": big for i in range(3)}
    files |= {"tests/test_a.py": "def test_a():\n    pass\n", "docs/a.md": "x\n"}
    focus = [f"src/pkg/big{i}.py" for i in range(3)]
    text, _ = build_repo_map(
        git_repo(tmp_path, files), test_cmd="task test", max_chars=1500, depth=3, cache_dir=None, focus=focus
    )
    assert len(text) <= 1500 and text.endswith("[map truncated]")
    for d in ("src", "tests", "docs"):
        assert any(line.startswith(f"{d}/ (") for line in _lines(text))
    assert "## In scope" in text and "src/pkg/big0.py:" in text  # the outline got what the directories left
    assert text.index("## In scope") < text.index("src/ (")
