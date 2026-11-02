"""The gate's ruff steps on the tester's files, on fake git repositories: fixed where ruff can fix them, a
finding where it cannot, and no file the layer did not change touched."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from airlie.story.tester_lint import LayerLint, RuffStep, lint_for, lint_note, ruff_steps
from tests.unit.git_helpers import git
from tests.unit.test_story_layer_checks import commit, repo

RUFF = f"{shlex.quote(sys.executable)} -m ruff"
GATE = (f"{RUFF} check .", f"{RUFF} format --check .", f"{sys.executable} -m pytest -q")
RUFF_TOML = 'line-length = 60\n\n[lint]\nselect = ["E", "F", "I"]\n'
MESSY = "x=1\n"
"""A file of the base that ruff format would change: not the tester's, so never touched."""
UNSORTED = "import sys\nimport os\n\n\ndef test_x():\n    assert os.sep and len(sys.argv)>0\n"
SORTED = "import os\nimport sys\n\n\ndef test_x():\n    assert os.sep and len(sys.argv) > 0\n"
LONG = "def test_long():\n    # " + "words " * 12 + "end\n    assert True\n"
"""A comment too long for the line length: ruff format does not wrap comments."""


def lint() -> LayerLint:
    found = lint_for(GATE, timeout_s=120)
    assert found is not None
    return found


def base_repo(tmp_path: Path) -> tuple[Path, str]:
    return repo(
        tmp_path, {"ruff.toml": RUFF_TOML, "messy.py": MESSY, "calc.py": "def add(a, b):\n    return a + b\n"}
    )


def test_the_ruff_steps_are_read_from_the_gate_and_nothing_else() -> None:
    steps = ruff_steps(
        (
            "uv sync --frozen",
            "uv run ruff check .",
            "uv run ruff format --check .",
            "uv run ruff check --fix .",  # already a fixer: not a check
            "uv run ruff format .",  # rewrites, checks nothing
            "uv run mypy",
            "uv run ruff check . && rm -rf build",  # not a plain ruff command
            "uv run pytest -q",
        )
    )
    assert steps == (
        RuffStep("uv run ruff check .", "uv run ruff", "check"),
        RuffStep("uv run ruff format --check .", "uv run ruff", "format"),
    )
    assert lint_for(("uv run mypy", "uv run pytest -q"), timeout_s=1) is None


def test_a_step_keeps_its_ruff_and_runs_on_the_files_given_without_a_cache() -> None:
    check, fmt = ruff_steps(("uv run ruff check .", "uv run ruff format --check src tests"))
    assert check.fixer("a.py") == "uv run ruff check --fix --no-cache --force-exclude -- a.py"
    assert check.check("a.py") == "uv run ruff check --no-cache --force-exclude -- a.py"
    assert fmt.fixer("a.py") == "uv run ruff format --no-cache --force-exclude -- a.py"
    assert fmt.check("a.py") == "uv run ruff format --check --no-cache --force-exclude -- a.py"


async def test_what_ruff_can_fix_in_the_testers_files_is_fixed_and_nothing_else_is_touched(
    tmp_path: Path,
) -> None:
    root, base = base_repo(tmp_path)
    commit(root, "tests", {"tests/test_x.py": UNSORTED, "tests/notes.txt": "x=1\n"})
    assert await lint().fix(root, base) == ["tests/test_x.py"]
    assert (root / "tests" / "test_x.py").read_text() == SORTED
    assert (root / "messy.py").read_text() == MESSY
    assert await lint().check(root, base) == []
    assert not (root / ".ruff_cache").exists()
    assert git(root, "status", "--porcelain") == "M tests/test_x.py"  # fixed, left for the pipeline to commit


async def test_what_ruff_cannot_fix_is_a_finding_naming_the_step_the_file_and_the_rule(
    tmp_path: Path,
) -> None:
    root, base = base_repo(tmp_path)
    commit(root, "tests", {"tests/test_long.py": LONG})
    assert await lint().fix(root, base) == []
    [found] = await lint().check(root, base)
    assert (found.kind, found.test) == ("lint_fails", GATE[0])
    assert "fix these by hand" in found.why
    assert "tests/test_long.py" in found.why and "E501" in found.why


async def test_a_layer_that_changed_no_python_file_has_nothing_to_lint(tmp_path: Path) -> None:
    root, base = base_repo(tmp_path)
    commit(root, "docs", {"README.md": "x=1\n"})
    assert await lint().fix(root, base) == []
    assert await lint().check(root, base) == []


def test_the_lint_note_names_the_repositorys_line_length_and_what_ruff_cannot_fix(tmp_path: Path) -> None:
    """AIR-48 and AIR-47, 2026-11-01: four package tries came back lint_fails on E501 (strings and docstrings
    of 111-113 characters against 110) and E741 (`l`), which ruff's fixer cannot mend, and the architect had
    never been told the line length. The note reads it as ruff would: ruff.toml before pyproject.toml."""
    root, base = repo(
        tmp_path, {"ruff.toml": RUFF_TOML, "pyproject.toml": "[tool.ruff]\nline-length = 110\n"}
    )
    note = lint_note(root, base, GATE)
    assert "60 characters" in note and "110" not in note
    assert "E741" in note and "`l`" in note
    assert "string" in note and "docstring" in note


def test_the_lint_note_reads_pyproject_falls_back_to_ruffs_default_and_is_empty_without_a_ruff_step(
    tmp_path: Path,
) -> None:
    root, base = repo(
        tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n\n[tool.ruff]\nline-length = 110\n"}
    )
    assert "110 characters" in lint_note(root, base, GATE)
    (tmp_path / "bare").mkdir()
    bare, bare_base = repo(tmp_path / "bare", {"calc.py": "x = 1\n"})
    assert "88 characters" in lint_note(bare, bare_base, GATE)
    assert lint_note(root, base, (f"{sys.executable} -m pytest -q",)) == ""
