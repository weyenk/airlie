"""The gate's ruff steps on the tester's files, on fake git repositories: fixed where ruff can fix them, a
finding where it cannot, and no file the layer did not change touched."""

from __future__ import annotations

import ast
import shlex
import sys
from pathlib import Path

from airlie.story.tester_lint import (
    LayerLint,
    RuffStep,
    async_awaiting_defs,
    lint_for,
    lint_note,
    ruff_steps,
    wrap_long_lines,
)
from tests.unit.git_helpers import git
from tests.unit.test_story_layer_checks import commit, repo

RUFF = f"{shlex.quote(sys.executable)} -m ruff"
GATE = (f"{RUFF} check .", f"{RUFF} format --check .", f"{sys.executable} -m pytest -q")
RUFF_TOML = 'line-length = 60\n\n[lint]\nselect = ["E", "F", "I"]\n'
MESSY = "x=1\n"
"""A file of the base that ruff format would change: not the tester's, so never touched."""
UNSORTED = "import sys\nimport os\n\n\ndef test_x():\n    assert os.sep and len(sys.argv)>0\n"
SORTED = "import os\nimport sys\n\n\ndef test_x():\n    assert os.sep and len(sys.argv) > 0\n"
LONG = 'def test_long():\n    message = "' + "words " * 12 + 'end"\n    assert message\n'
"""A string too long for the line length: neither ruff nor airlie splits a string."""


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
    assert check.fixer("a.py") == "uv run ruff check --fix --unsafe-fixes --no-cache --force-exclude -- a.py"
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


UNUSED_AND_AMBIGUOUS = """from collections import namedtuple

Layer = namedtuple("Layer", "title l")


def helper(n):
    return n


def test_positions():
    layers = [Layer("a", 1), Layer("b", 2)]
    top = layers[-1]
    body = "a b"
    positions = [body.index(l.title) for l in layers]
    assert positions == sorted(positions)
    for l in layers:
        assert l.l in (1, 2)
    assert helper(n=3) == 3
"""
"""AIR-47 layer 02, 2026-11-02: a package came back lint_fails on F841 (`top`, never used) and E741 (`l` in a
comprehension and a loop), neither of which `ruff check --fix` mends."""


async def test_an_unused_local_and_an_ambiguous_name_are_fixed_and_nothing_else_is(tmp_path: Path) -> None:
    root, base = base_repo(tmp_path)
    commit(root, "tests", {"tests/test_pos.py": UNUSED_AND_AMBIGUOUS})
    assert await lint().fix(root, base) == ["tests/test_pos.py"]
    fixed = (root / "tests" / "test_pos.py").read_text()
    assert "top =" not in fixed
    assert "[body.index(l_.title) for l_ in layers]" in fixed
    assert "for l_ in layers:" in fixed and "assert l_.l in (1, 2)" in fixed  # the attribute keeps its name
    assert 'namedtuple("Layer", "title l")' in fixed and "helper(n=3)" in fixed
    assert await lint().check(root, base) == []


UNIMPORTED = '''"""Added tests."""

from pathlib import Path


async def no_ghstack(args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
    raise PublishError("`gh stack --help` exited 1")


def test_twice() -> None:
    assert Twice and no_ghstack
'''
"""AIR-47 layer 02, 2026-11-02: the architect's test additions used `Mapping` and `PublishError` unimported
(F821, which no ruff fix mends), and the package came back lint_fails until the story blocked."""


async def test_an_undefined_name_gets_its_import_when_it_comes_from_one_known_place(tmp_path: Path) -> None:
    root, base = base_repo(tmp_path)
    commit(
        root,
        "sources",
        {
            "src/pkg/__init__.py": "",
            "src/pkg/publish.py": "class PublishError(Exception):\n    pass\n",
            "src/pkg/a.py": "Twice = 2\n",
            "src/pkg/b.py": "def Twice():\n    return 2\n",  # a second definer: no import is guessed
            "tests/test_helpers.py": "class PublishError(Exception):\n    pass\n",  # tests are not a source
        },
    )
    base = git(root, "rev-parse", "HEAD")
    commit(root, "tests", {"tests/test_publish.py": UNIMPORTED})
    assert await lint().fix(root, base) == ["tests/test_publish.py"]
    fixed = (root / "tests" / "test_publish.py").read_text()
    assert (
        "from collections.abc import Mapping\n" in fixed and "from pkg.publish import PublishError\n" in fixed
    )
    assert fixed.startswith('"""Added tests."""\n') and "import Twice" not in fixed
    [found] = await lint().check(root, base)
    assert "Undefined name `Twice`" in found.why
    assert "Mapping" not in found.why and "PublishError" not in found.why


def test_an_import_goes_after_the_last_import_or_the_docstring_and_is_not_repeated() -> None:
    from airlie.story.tester_lint import import_for, with_imports

    assert with_imports('"""Doc."""\nx = Path()\n', ["from pathlib import Path"]) == (
        '"""Doc."""\nfrom pathlib import Path\nx = Path()\n'
    )
    held = "import os\n\nx = os.sep\n"
    assert with_imports(held, ["import os"]) == held
    assert with_imports("import os\n\ny = json.dumps(1)\n", ["import json"]) == (
        "import os\nimport json\n\ny = json.dumps(1)\n"
    )
    assert import_for("Any", {}) == "from typing import Any" and import_for("json", {}) == "import json"
    assert import_for("Thing", {"Thing": {"pkg.m"}}) == "from pkg.m import Thing"
    assert import_for("Thing", {"Thing": {"pkg.m", "pkg.n"}}) is None and import_for("Thing", {}) is None


def test_a_parameter_or_a_module_name_is_not_renamed_and_a_taken_name_is_not_reused() -> None:
    from airlie.story.tester_lint import rename_ambiguous

    source = (
        "l = 1\n\n\ndef f(I):\n    return I\n\n\ndef g(xs):\n    l_ = 0\n    return [l for l in xs] + [l_]\n"
    )
    renamed = rename_ambiguous(source)
    assert renamed.startswith("l = 1\n") and "def f(I):\n    return I\n" in renamed
    assert "[l_2 for l_2 in xs] + [l_]" in renamed
    assert rename_ambiguous("def broken(:\n") == "def broken(:\n"


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


AIR59_LINE = '    carries "type": "story" so parse_story accepts it and _refine reaches the comment and the section write."""'  # noqa: E501
"""AIR-59 layer 02, 2026-11-05: a docstring line of 111 characters against the repository's 110 sent the
package back twice as lint_fails, a whole try each."""
AIR59 = f'''def test_refine_writes_the_section():
    """A card whose text holds the refined story's JSON block, as the board returns it after a refine: it
{AIR59_LINE}
    pass
'''


def test_the_air59_docstring_line_is_wrapped_at_a_word_keeping_its_indentation() -> None:
    assert len(AIR59_LINE) == 111
    wrapped = wrap_long_lines(AIR59, 110)
    lines = wrapped.splitlines()
    assert max(len(x) for x in lines) <= 110
    assert lines[2].startswith('    carries "type": "story"') and lines[3].startswith("    ")
    assert lines[3].endswith('write."""')
    assert " ".join(wrapped.split()) == " ".join(AIR59.split())  # the same words, only the breaks moved


def test_a_long_comment_is_wrapped_and_a_trailing_one_continues_on_the_lines_below() -> None:
    source = (
        "def test_x():\n    # " + "words " * 12 + "end\n    x = 1  # " + "more " * 12 + "end\n    assert x\n"
    )
    lines = wrap_long_lines(source, 40).splitlines()
    assert max(len(x) for x in lines) <= 40
    comments = [x for x in lines if x.strip().startswith("#")]
    assert all(x.startswith("    # ") for x in comments)
    assert any(x.startswith("    x = 1  # more") for x in lines)
    assert " ".join(" ".join(lines).replace("#", "").split()) == " ".join(source.replace("#", "").split())


def test_code_a_string_a_directive_comment_and_a_doctest_are_never_wrapped() -> None:
    source = (
        "def test_x():\n"
        '    """Doc.\n\n    >>> ' + "f(1) + " * 10 + 'f(2)\n    """\n'
        '    message = "' + "words " * 12 + 'end"\n'
        '    note = """\n    ' + "text " * 12 + 'end\n    """\n'
        "    assert message, note  # noqa: " + "E501 " * 10 + "\n"
        "    assert " + " and ".join(["message"] * 8) + "\n"
    )
    assert wrap_long_lines(source, 40) == source
    broken = "def broken(:\n    # " + "words " * 20 + "\n"
    assert wrap_long_lines(broken, 40) == broken


async def test_the_fixer_wraps_a_long_docstring_or_comment_at_the_repositorys_line_length(
    tmp_path: Path,
) -> None:
    root, base = repo(
        tmp_path, {"pyproject.toml": '[tool.ruff]\nline-length = 110\n\n[tool.ruff.lint]\nselect = ["E"]\n'}
    )
    commit(root, "tests", {"tests/test_refine.py": AIR59 + "\n\n# " + "words " * 20 + "end\n"})
    assert await lint().fix(root, base) == ["tests/test_refine.py"]
    fixed = (root / "tests" / "test_refine.py").read_text()
    assert max(len(x) for x in fixed.splitlines()) <= 110 and 'write."""' in fixed
    assert await lint().check(root, base) == []


AWAITING = '''"""Refine tests."""

import asyncio


class Board:
    def __init__(self):
        self.sections = []


async def plan(board):
    await asyncio.sleep(0)
    board.sections.append("refined")
    return board


def _refined(board):
    """The board after a refine.

def not_this_one():
    """
    with open(__file__):
        st = await plan(
            board,
        )
    return st


async def test_refine_posts_no_comment():
    board = _refined(Board())
    assert board.sections == ["refined"]


async def test_refine_writes_the_section():
    _refined(Board())
    assert len(_refined(Board()).sections) == 1


def test_a_plain_caller():
    assert _refined
'''
"""AIR-59 layer 01, 2026-11-04 and 2026-11-06: a test helper written as a plain `def` whose body awaits the
pipeline's plan, so the module did not parse ("'await' outside async function") and the package came back
unparseable_test, a whole try each time."""


def test_a_plain_def_that_awaits_becomes_async_and_its_whole_statement_calls_are_awaited() -> None:
    fixed = async_awaiting_defs(AWAITING)
    ast.parse(fixed)
    assert "\nasync def _refined(board):\n" in fixed
    assert '\ndef not_this_one():\n    """' in fixed  # in the docstring: not a def
    assert "    board = await _refined(Board())\n" in fixed
    assert "    await _refined(Board())\n" in fixed
    assert "    assert len(_refined(Board()).sections) == 1\n" in fixed  # not a whole statement: left
    assert "\ndef test_a_plain_caller():\n    assert _refined\n" in fixed  # not an async caller: left
    assert fixed.count("async def") == AWAITING.count("async def") + 1


def test_an_async_def_and_a_file_that_parses_or_breaks_otherwise_are_left_unchanged() -> None:
    parses = "async def helper():\n    return await thing()\n\n\ndef test_x():\n    assert helper\n"
    assert async_awaiting_defs(parses) == parses
    unrelated = "def test_x(:\n    assert 1\n"
    assert async_awaiting_defs(unrelated) == unrelated
    also_broken = "def helper():\n    return await thing()\n\n\ndef test_x():\n    assert (1\n"
    assert async_awaiting_defs(also_broken) == also_broken
    in_a_lambda = "async def outer():\n    f = lambda: await thing()\n    return f\n"
    assert async_awaiting_defs(in_a_lambda) == in_a_lambda
    lambda_in_a_def = "def outer():\n    f = lambda: await thing()\n    return f\n"
    assert async_awaiting_defs(lambda_in_a_def) == lambda_in_a_def  # async outer would not mend the lambda


def test_only_the_def_whose_own_body_awaits_becomes_async() -> None:
    inner = "def outer():\n    def inner():\n        return await thing()\n\n    return inner\n"
    assert async_awaiting_defs(inner) == inner.replace("    def inner", "    async def inner")
    outer = (
        "def outer():\n    async def inner():\n        return await thing()\n\n"
        "    if True:\n        x = 1\n    else:\n        await inner()\n"
    )
    assert async_awaiting_defs(outer) == "async " + outer


async def test_the_fixer_makes_an_awaiting_helper_async_before_anything_else(tmp_path: Path) -> None:
    toml = '[tool.ruff]\nline-length = 110\n\n[tool.ruff.lint]\nselect = ["E", "F"]\n'
    root, base = repo(tmp_path, {"pyproject.toml": toml})
    commit(root, "tests", {"tests/test_refine.py": AWAITING})
    assert await lint().fix(root, base) == ["tests/test_refine.py"]
    fixed = (root / "tests" / "test_refine.py").read_text()
    assert "async def _refined(board):" in fixed and "board = await _refined(Board())" in fixed
    assert await lint().check(root, base) == []
