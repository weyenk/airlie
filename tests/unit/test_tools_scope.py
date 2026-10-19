"""Scoped run_tests: the layer's own tests first, the whole gate once those pass (airlie.tools.scope)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.scope import (
    GateMemo,
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


def ctx(
    repo: Path,
    base: str,
    memo: GateMemo | None = None,
    steps: tuple[str, ...] = (PYTEST,),
    check_base: str = "",
) -> ToolContext:
    return ToolContext(
        worktree=repo,
        shell_timeout_s=120.0,
        test_cmd=" && ".join(steps),
        test_steps=steps,
        scope_base=base,
        gate_memo=memo,
        check_base=check_base,
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


async def test_when_the_changed_tests_pass_the_whole_suite_runs_and_can_still_fail(tmp_path: Path) -> None:
    repo, base = repo_with(tmp_path, {"tests/test_old.py": FAIL})
    (repo / "tests/test_new.py").write_text(PASS)
    memo = GateMemo()
    result = await RunTests().run({}, ctx(repo, base, memo))
    assert not result.ok
    assert "your tests pass; the whole suite FAILS" in result.output and "test_bad" in result.output
    assert memo.passed is None


async def test_a_tree_that_passed_the_whole_suite_is_not_run_again(tmp_path: Path) -> None:
    repo, base = repo_with(tmp_path, {"tests/test_old.py": PASS})
    (repo / "tests/test_new.py").write_text(PASS.replace("ok", "new_ok"))
    memo = GateMemo()
    first = await RunTests().run({}, ctx(repo, base, memo))
    assert first.ok and "the whole suite passes" in first.output
    assert memo.passed == tree_fingerprint(repo)
    again = await RunTests().run({}, ctx(repo, base, memo))
    assert again.ok and "already passed on exactly this tree" in again.output
    (repo / "tests/test_new.py").write_text(PASS.replace("ok", "other"))  # an edit: it runs again
    third = await RunTests().run({}, ctx(repo, base, memo))
    assert third.ok and "the whole suite passes" in third.output


async def test_with_no_test_file_of_its_own_run_tests_fails_and_says_to_write_them(tmp_path: Path) -> None:
    repo, base = repo_with(tmp_path, {"tests/test_old.py": PASS})
    result = await RunTests().run({}, ctx(repo, base, GateMemo(), steps=("echo lint-ok", PYTEST)))
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
    result = await RunTests().run({}, ctx(repo, base, GateMemo(), steps=steps))
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
    result = await RunTests().run({}, ctx(repo, base, GateMemo(), steps=steps))
    assert result.ok, result.output
    assert "x = 1 if a else 2" in (repo / "mod.py").read_text()


# ---- the red check, at the step the test is written ----

CALC = "def add(a, b):\n    return a + b\n"
MUL = CALC + "def mul(a, b):\n    return a * b\n"
TEST_MUL = "def test_mul():\n    from calc import mul\n\n    assert mul(2, 3) == 6\n"
TEST_ADD_AGAIN = "from calc import add\n\n\ndef test_add_again():\n    assert add(2, 2) == 4\n"
CHECKED = "these also pass with your change undone"


def layer(tmp_path: Path, tests: str) -> tuple[Path, str]:
    """A repository at a base holding calc.py, with the layer's uncommitted change: mul, and `tests`."""
    repo, base = repo_with(tmp_path, {"pytest.ini": "[pytest]\n", "calc.py": CALC})
    (repo / "calc.py").write_text(MUL)
    (repo / "tests").mkdir()
    (repo / "tests/test_calc.py").write_text(tests)
    return repo, base


async def test_a_green_run_tests_names_the_new_tests_that_pass_without_the_change(tmp_path: Path) -> None:
    repo, base = layer(tmp_path, TEST_MUL + "\n\n" + TEST_ADD_AGAIN)
    result = await RunTests().run({}, ctx(repo, base, GateMemo(), check_base=base))
    assert result.ok, result.output
    assert "the whole suite passes" in result.output
    block = result.output[result.output.index("airlie checked the tests you added or changed") :]
    assert CHECKED in block and "- tests/test_calc.py::test_add_again" in block
    assert "test_mul" not in block
    assert "put that check in the same test that triggers the behaviour" in block
    # the worktree is the layer's again: the change there, nothing left over from the check
    assert (repo / "calc.py").read_text() == MUL and git(repo, "status", "--porcelain") == ""


async def test_a_green_run_tests_says_nothing_when_every_new_test_fails_without_the_change(
    tmp_path: Path,
) -> None:
    repo, base = layer(tmp_path, TEST_MUL)
    result = await RunTests().run({}, ctx(repo, base, GateMemo(), check_base=base))
    assert result.ok and "the whole suite passes" in result.output
    assert "airlie checked" not in result.output and CHECKED not in result.output
    assert (repo / "calc.py").read_text() == MUL and git(repo, "status", "--porcelain") == ""


async def test_without_a_check_base_a_green_run_tests_runs_no_check(tmp_path: Path) -> None:
    repo, base = layer(tmp_path, TEST_MUL + "\n\n" + TEST_ADD_AGAIN)
    result = await RunTests().run({}, ctx(repo, base, GateMemo()))
    assert result.ok and "airlie checked" not in result.output
    assert git(repo, "rev-parse", "HEAD") == base  # nothing was committed for a check


async def test_a_red_run_tests_runs_no_check(tmp_path: Path) -> None:
    repo, base = layer(tmp_path, TEST_ADD_AGAIN + "\n\n" + FAIL)
    result = await RunTests().run({}, ctx(repo, base, GateMemo(), check_base=base))
    assert not result.ok and "airlie checked" not in result.output
    assert git(repo, "rev-parse", "HEAD") == base


async def test_a_check_that_crashes_leaves_the_worktree_as_it_was_and_the_run_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import airlie.story.layer_checks as layer_checks

    async def boom(*a: object, **kw: object) -> ToolResult:
        raise RuntimeError("pytest exploded")

    monkeypatch.setattr(layer_checks, "run_cmd", boom)  # inside the check, after the change is put back
    repo, base = layer(tmp_path, TEST_MUL + "\n\n" + TEST_ADD_AGAIN)
    result = await RunTests().run({}, ctx(repo, base, GateMemo(), check_base=base))
    assert result.ok and "the whole suite passes" in result.output
    assert "airlie checked" not in result.output
    assert (repo / "calc.py").read_text() == MUL and git(repo, "status", "--porcelain") == ""
