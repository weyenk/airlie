"""Scoped run_tests routes a layer's JS/TS test files to vitest, never to pytest (airlie.tools.scope).

A `.test.ts` file is a vitest test, not a pytest module: handing it to pytest makes pytest collect nothing
(exit 5, "no tests ran"). A pytest step therefore runs only the Python modules in the layer's changed files,
and a JS/TS test file runs under vitest in its own package instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.tools.scope import _js_package_dir, scope_step, scoped_steps


@pytest.fixture
def web_pkg(tmp_path: Path) -> tuple[Path, str]:
    """A worktree with a JS/TS test in a package (web/package.json) and a Python module elsewhere."""
    pkg = tmp_path / "web"
    (pkg / "src" / "components").mkdir(parents=True)
    (pkg / "package.json").write_text('{"name": "web"}\n')
    (pkg / "src" / "components" / "SafeMarkdown.test.ts").write_text("test('x', () => {})\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n")
    return tmp_path, "web/src/components/SafeMarkdown.test.ts"


def test_a_pytest_step_gets_only_the_python_modules_and_drops_the_js_test(web_pkg: tuple[Path, str]) -> None:
    worktree, js = web_pkg
    # A Python module goes to pytest; the JS/TS file is not handed to pytest.
    assert (
        scope_step("uv run pytest -q", ["tests/test_a.py", js], worktree)
        == "uv run pytest -q tests/test_a.py"
    )
    # A lone JS/TS file is not a pytest module: the step becomes a vitest run, not an empty pytest.
    step = scope_step("uv run pytest -q", [js], worktree)
    assert step is not None
    assert "vitest" in step and "SafeMarkdown.test.ts" in step


def test_a_pytest_step_with_only_js_tests_becomes_a_vitest_run_in_their_package(
    web_pkg: tuple[Path, str],
) -> None:
    worktree, js = web_pkg
    step = scope_step("uv run pytest -q", [js], worktree)
    assert step is not None
    assert "vitest" in step
    assert "SafeMarkdown.test.ts" in step  # the file, relative to its package (web)


def test_a_pytest_step_with_no_test_file_at_all_is_left_out() -> None:
    assert scope_step("uv run pytest -q", []) is None


def test_a_non_pytest_step_is_left_alone(web_pkg: tuple[Path, str]) -> None:
    worktree, js = web_pkg
    assert scope_step("uv run ruff check .", [js], worktree) == "uv run ruff check ."


def test_scoped_steps_runs_the_js_tests_under_vitest(web_pkg: tuple[Path, str]) -> None:
    worktree, js = web_pkg
    out = scoped_steps(["uv run pytest -q"], [js], worktree)
    assert len(out) == 1
    assert "vitest" in out[0] and "SafeMarkdown.test.ts" in out[0]


def test_the_js_package_dir_walks_up_to_the_nearest_package_json(web_pkg: tuple[Path, str]) -> None:
    worktree, js = web_pkg
    assert _js_package_dir(worktree, js) == "web"
    # A file with no package.json up its tree has no package to run in.
    (worktree / "src" / "components").mkdir(parents=True)
    assert _js_package_dir(worktree, "src/components/thing.test.ts") is None
