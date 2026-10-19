"""Scoped run_tests: a story layer's own tests, never the whole suite, which is the pipeline's after review
(airlie.tools.scope)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from airlie.tools.protocol import ToolContext
from airlie.tools.scope import (
    autofix_steps,
    changed_test_files,
    scope_step,
    scoped_steps,
    tree_fingerprint,
)
from airlie.tools.shell import RunTests

PASS = "def test_ok():\n    assert True\n"
FAIL = "def test_bad():\n    assert False\n"
PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def repo_with(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "wt"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def ctx(repo: Path, base: str, steps: tuple[str, ...] = (PYTEST,)) -> ToolContext:
    return ToolContext(
        worktree=repo,
        shell_timeout_s=120.0,
        test_cmd=" && ".join(steps),
        test_steps=steps,
        scope_base=base,
    )


def test_a_pytest_step_gets_the_files_and_other_steps_are_left_alone() -> None:
    assert scope_step("uv run pytest -q", ["tests/test_a.py", "tests/my test.py"]) == (
        "uv run pytest -q tests/test_a.py 'tests/my test.py'"
    )
    assert scope_step("uv run ruff check .", ["tests/test_a.py"]) == "uv run ruff check ."


def test_a_pytest_step_with_no_file_to_run_is_left_out() -> None:
    assert scope_step("uv run pytest -q", []) is None
    assert scoped_steps(["uv sync", "uv run pytest -q", "uv run mypy"], []) == ["uv sync", "uv run mypy"]
    assert scoped_steps(["uv sync", "uv run pytest -q"], ["tests/t.py"]) == [
        "uv sync",
        "uv run pytest -q tests/t.py",
    ]


def test_changed_test_files_are_the_test_modules_added_or_changed_since_the_base(tmp_path: Path) -> None:
    repo, base = repo_with(
        tmp_path,
        {
            "tests/test_keep.py": PASS,
            "tests/test_edit.py": PASS,
            "tests/test_gone.py": PASS,
            "src/m.py": "x = 1\n",
        },
    )
    (repo / "tests/test_edit.py").write_text(PASS + "\n# edited\n")
    (repo / "tests/test_gone.py").unlink()
    (repo / "tests/test_new.py").write_text(PASS)  # untracked
    (repo / "tests/conftest.py").write_text("")  # a test-side file, not a test module
    (repo / "src/m.py").write_text("x = 2\n")
    git(repo, "add", "tests/test_edit.py")  # staged, uncommitted
    assert changed_test_files(repo, base) == ["tests/test_edit.py", "tests/test_new.py"]
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "wip")
    assert changed_test_files(repo, base) == [
        "tests/test_edit.py",
        "tests/test_new.py",
    ]  # committed counts too


def test_a_tree_fingerprint_follows_the_files_and_ignores_what_git_ignores(tmp_path: Path) -> None:
    repo, _ = repo_with(tmp_path, {"a.py": "x = 1\n", ".gitignore": "junk/\n"})
    first = tree_fingerprint(repo)
    assert first and tree_fingerprint(repo) == first
    (repo / "junk").mkdir()
    (repo / "junk/cache").write_text("noise")
    assert tree_fingerprint(repo) == first
    (repo / "a.py").write_text("x = 2\n")
    changed = tree_fingerprint(repo)
    assert changed and changed != first
    (repo / "a.py").write_text("x = 1\n")
    assert tree_fingerprint(repo) == first  # the same files, the same fingerprint


async def test_run_tests_runs_only_the_changed_tests_until_they_pass(tmp_path: Path) -> None:
    repo, base = repo_with(tmp_path, {"tests/test_old.py": FAIL})
    (repo / "tests/test_new.py").write_text(FAIL.replace("bad", "new_bad"))
    result = await RunTests().run({}, ctx(repo, base))
    assert not result.ok
    assert "scoped to your 1 test file(s)" in result.output
    assert "test_new_bad" in result.output and "test_bad" not in result.output.replace("test_new_bad", "")


async def test_when_the_changed_tests_pass_run_tests_passes_without_the_whole_suite(tmp_path: Path) -> None:
    """Noah, 2026-10-19: once the layer's own tests pass, the subtask is done; the whole suite is the
    pipeline's final gate, after review. A red test elsewhere does not hold the coder back here."""
    repo, base = repo_with(tmp_path, {"tests/test_old.py": FAIL})
    (repo / "tests/test_new.py").write_text(PASS)
    result = await RunTests().run({}, ctx(repo, base))
    assert result.ok, result.output
    assert "scoped to your 1 test file(s); the pipeline runs the whole suite after review" in result.output
    assert "test_bad" not in result.output and "1 passed" in result.output


async def test_the_scoped_run_still_runs_the_gates_other_steps_on_everything(tmp_path: Path) -> None:
    repo, base = repo_with(tmp_path, {"tests/test_old.py": PASS})
    (repo / "tests/test_new.py").write_text(PASS.replace("ok", "new_ok"))
    result = await RunTests().run({}, ctx(repo, base, steps=("echo lint-ok", "false", PYTEST)))
    assert not result.ok and "lint-ok" in result.output and "FAIL (exit 1): false" in result.output


async def test_with_no_test_file_of_its_own_run_tests_fails_and_says_to_write_them(tmp_path: Path) -> None:
    repo, base = repo_with(tmp_path, {"tests/test_old.py": PASS})
    result = await RunTests().run({}, ctx(repo, base, steps=("echo lint-ok", PYTEST)))
    assert not result.ok
    assert "lint-ok" in result.output and "write the layer's tests first" in result.output
    assert "passed" not in result.output.replace("gate commands pass", "")  # pytest never ran


async def test_without_a_scope_base_run_tests_runs_the_whole_gate_as_before(tmp_path: Path) -> None:
    repo, base = repo_with(tmp_path, {"tests/test_old.py": FAIL})
    result = await RunTests().run({}, ctx(repo, ""))
    assert not result.ok and "scoped to" not in result.output and "test_bad" in result.output


def test_ruff_steps_get_a_fixing_twin_and_other_steps_none() -> None:
    steps = ["uv sync --frozen", "uv run ruff check .", "uv run ruff format --check .", "uv run mypy"]
    assert autofix_steps(steps) == ["uv run ruff check . --fix --unsafe-fixes", "uv run ruff format ."]
    assert autofix_steps(["uv run ruff check . --fix"]) == []
    assert autofix_steps(["make lint", "pnpm test"]) == []


async def test_run_tests_formats_the_code_before_the_gate_checks_it(tmp_path: Path) -> None:
    repo, base = repo_with(tmp_path, {"tests/test_old.py": PASS})
    (repo / "tests/test_new.py").write_text(PASS)
    (repo / "mod.py").write_text("x   =   1\n")  # unformatted
    steps = ("uv run ruff format --check .", PYTEST)
    result = await RunTests().run({}, ctx(repo, base, steps=steps))
    assert result.ok, result.output
    assert (repo / "mod.py").read_text() == "x = 1\n"


async def test_run_tests_mends_what_ruff_only_fixes_as_unsafe_before_the_gate_checks_it(
    tmp_path: Path,
) -> None:
    repo, base = repo_with(tmp_path, {"tests/test_old.py": PASS})
    (repo / "tests/test_new.py").write_text(PASS)
    (repo / "mod.py").write_text(
        "def pick(a):\n    if a:\n        x = 1\n    else:\n        x = 2\n    return x\n"
    )  # SIM108: ruff offers its ternary only as an unsafe fix
    (repo / "ruff.toml").write_text('[lint]\nselect = ["SIM108"]\n')
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "mod")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tests/test_new.py").write_text(PASS.replace("ok", "new_ok"))
    steps = ("uv run ruff check .", PYTEST)
    result = await RunTests().run({}, ctx(repo, base, steps=steps))
    assert result.ok, result.output
    assert "x = 1 if a else 2" in (repo / "mod.py").read_text()
