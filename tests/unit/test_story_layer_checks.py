"""The checks a layer's new and changed tests must pass, on fake git repositories: each fails on the layer's
base (red check, run with pytest) and asserts something (AST)."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from airlie.story.layer_checks import (
    Finding,
    PytestChecks,
    changed_tests,
    checks_for,
    feedback,
    pytest_runner,
)
from tests.unit.git_helpers import git

PYTEST = f"{shlex.quote(sys.executable)} -m pytest"
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


async def findings(root: Path, base: str) -> list[Finding]:
    return await PytestChecks(PYTEST, timeout_s=120).run(root, base)


# ---- which tests are new or changed ----


def test_only_new_and_changed_test_functions_are_picked(tmp_path: Path) -> None:
    root, base = repo(
        tmp_path,
        {
            "calc.py": CALC,
            "tests/test_calc.py": OLD_TESTS + "\n\ndef test_zero():\n    assert add(0, 0) == 0\n",
        },
    )
    layer = (
        "from calc import add, mul  # a new import: the file changed\n\n\n"
        "def test_add():\n    assert add(1,2)==3  # reformatted only: not changed\n\n\n"
        "def test_zero():\n    assert add(0, 0) == 0 and add(1, 0) == 1\n\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n\n\n"
        "class TestMul:\n    def test_one(self):\n        assert mul(1, 7) == 7\n\n"
        "    def helper(self):\n        pass\n\n\n"
        "def helper_not_a_test():\n    pass\n"
    )
    commit(
        root, "layer", {"tests/test_calc.py": layer, "calc.py": CALC + "def mul(a, b):\n    return a * b\n"}
    )
    ids = [t.id for t in changed_tests(root, base)]
    assert ids == [
        "tests/test_calc.py::test_zero",
        "tests/test_calc.py::test_mul",
        "tests/test_calc.py::TestMul::test_one",
    ]


def test_only_test_modules_are_read(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    commit(
        root,
        "layer",
        {
            "calc.py": CALC + "def test_like_name():\n    pass\n",  # production code: not a test module
            "tests/helpers.py": "def test_helper():\n    pass\n",  # not a test module either
            "tests/calc_test.py": "def test_suffix_style():\n    assert True\n",
            "web/app.ts": "test('x', () => {})\n",
        },
    )
    assert [t.id for t in changed_tests(root, base)] == ["tests/calc_test.py::test_suffix_style"]


# ---- no-assertion check ----


def test_a_test_with_no_assertion_is_a_finding(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    tests = (
        "import pytest\nfrom unittest import mock\nfrom calc import add\n\n\n"
        "def check_sum(v):\n    assert v == 3\n\n\n"
        "def test_runs_only():\n    add(1, 2)\n\n\n"
        "def test_asserts():\n    assert add(1, 2) == 3\n\n\n"
        "def test_raises():\n    with pytest.raises(TypeError):\n        add(1)\n\n\n"
        "def test_warns():\n    with pytest.warns(UserWarning):\n        add(1, 2)\n\n\n"
        "def test_mock_assert():\n    m = mock.Mock()\n    m(1)\n    m.assert_called_once_with(1)\n\n\n"
        "def test_helper_asserts():\n    check_sum(add(1, 2))\n\n\n"
        "def test_raise_on_mismatch():\n    if add(1, 2) != 3:\n        raise AssertionError('sum')\n\n\n"
        "class TestPrints:\n    def test_prints(self):\n        print(add(1, 2))\n"
    )
    commit(root, "layer", {"tests/test_calc.py": tests})
    free = [t.id for t in changed_tests(root, base) if not t.asserts]
    assert free == ["tests/test_calc.py::test_runs_only", "tests/test_calc.py::TestPrints::test_prints"]


async def test_assertion_free_tests_are_reported_with_why(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    commit(
        root,
        "layer",
        {
            "calc.py": CALC + "def mul(a, b):\n    return a * b\n",
            "tests/test_calc.py": "from calc import mul\n\n\ndef test_mul():\n    mul(2, 3)\n",
        },
    )
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("assertion_free_test", "tests/test_calc.py::test_mul")]
    assert "no assert" in found[0].why
    assert found[0].source == "def test_mul():\n    mul(2, 3)"


# ---- red check ----


async def test_a_test_that_passes_on_the_base_is_caught(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    head = commit(
        root,
        "layer",
        {
            "calc.py": CALC + "def mul(a, b):\n    return a * b\n",
            "tests/test_calc.py": (
                "from calc import add\n\n\n"
                "def test_add_again():\n    assert add(2, 2) == 4\n\n\n"
                "def test_mul():\n    from calc import mul\n\n    assert mul(2, 3) == 6\n"
            ),
        },
    )
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("vacuous_test", "tests/test_calc.py::test_add_again")]
    assert "passes on the layer's base" in found[0].why
    # the worktree is the layer's again, as it was
    assert git(root, "rev-parse", "HEAD") == head and git(root, "status", "--porcelain") == ""
    assert "def mul" in (root / "calc.py").read_text()


async def test_a_real_red_test_passes_the_check(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    commit(
        root,
        "layer",
        {
            "calc.py": CALC + "def mul(a, b):\n    return a * b\n",
            "src/new_module.py": "VALUE = 6\n",  # added since the base: gone while the check runs
            "tests/test_calc.py": "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
            "tests/test_value.py": (
                "import sys\nsys.path.insert(0, 'src')\n\n\n"
                "def test_value():\n    from new_module import VALUE\n\n    assert VALUE == 6\n"
            ),
        },
    )
    assert await findings(root, base) == []
    assert (root / "src/new_module.py").is_file() and git(root, "status", "--porcelain") == ""


async def test_a_changed_test_that_still_passes_on_the_base_is_caught_and_unrelated_ones_are_not_run(
    tmp_path: Path,
) -> None:
    root, base = repo(
        tmp_path,
        {
            "calc.py": CALC,
            "tests/test_calc.py": OLD_TESTS,
            "tests/test_other.py": "def test_other():\n    assert 1 + 1 == 2\n",
        },
    )
    commit(
        root,
        "layer",
        {
            "calc.py": "def add(a, b):\n    return int(a) + int(b)\n",
            # test_add changed: a new case the change makes pass; the old one still passes on the base
            "tests/test_calc.py": OLD_TESTS + "    assert add('1', '2') == 3\n",
            # reformatted only, and passing on the base: unrelated to the change, so never run or reported
            "tests/test_other.py": "def test_other():\n    assert 1+1 == 2\n",
        },
    )
    assert await findings(root, base) == []
    commit(root, "weaker", {"tests/test_calc.py": OLD_TESTS.replace("== 3", "== 3 or True")})
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("vacuous_test", "tests/test_calc.py::test_add")]


async def test_a_layer_with_no_new_or_changed_tests_has_no_findings(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    commit(root, "layer", {"calc.py": CALC + "# a comment\n"})
    assert await findings(root, base) == []


# ---- the seam: only a pytest gate gets these checks ----


def test_the_pytest_runner_comes_from_the_gate() -> None:
    assert pytest_runner(["uv sync --frozen", "uv run ruff check .", "uv run pytest -q"]) == "uv run pytest"
    assert pytest_runner(["python -m pytest tests -x"]) == "python -m pytest"
    assert pytest_runner(["make lint && pytest -q"]) == "pytest"
    assert pytest_runner(["/opt/venv/bin/pytest"]) == "/opt/venv/bin/pytest"
    assert pytest_runner(["pnpm install --frozen-lockfile", "pnpm test"]) is None
    assert pytest_runner(["echo 'unclosed"]) is None


def test_a_gate_without_pytest_gets_no_checks() -> None:
    assert checks_for(["pnpm test"], timeout_s=60) is None
    checks = checks_for(["uv run pytest -q"], timeout_s=60)
    assert checks is not None and checks.runner == "uv run pytest"


def test_feedback_names_each_test_and_why() -> None:
    text = feedback(
        [
            Finding(
                "vacuous_test",
                "tests/test_a.py::test_x",
                "it passes on the layer's base",
                "def test_x(): ...",
            ),
            Finding(
                "assertion_free_test", "tests/test_a.py::test_y", "it has no assert", "def test_y(): ..."
            ),
        ]
    )
    assert "- tests/test_a.py::test_x: it passes on the layer's base" in text
    assert "- tests/test_a.py::test_y: it has no assert" in text
    assert "run_tests" in text
