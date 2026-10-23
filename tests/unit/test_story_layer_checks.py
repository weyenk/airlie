"""The pre-code check a layer's tests must pass before any code is written, on fake git repositories: the
whole suite runs on the tester's tree, each new test fails there and every other test passes (pytest), and
each new test asserts something (AST)."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from airlie.prompts import text as hub_text
from airlie.story.layer_checks import (
    Finding,
    PytestChecks,
    amend_feedback,
    amend_feedback_said,
    changed_tests,
    checks_for,
    feedback,
    feedback_said,
    findings_text,
    is_testers_test,
    pytest_runner,
    static_findings,
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
        root, "tests", {"tests/test_calc.py": "from calc import mul\n\n\ndef test_mul():\n    mul(2, 3)\n"}
    )
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("assertion_free_test", "tests/test_calc.py::test_mul")]
    assert "no assert" in found[0].why
    assert found[0].source == "def test_mul():\n    mul(2, 3)"


# ---- the pre-code run: new tests red, the rest green ----

TEST_MUL = "def test_mul():\n    from calc import mul\n\n    assert mul(2, 3) == 6\n"
TEST_ADD_AGAIN = "def test_add_again():\n    from calc import add\n\n    assert add(2, 2) == 4\n"


async def test_a_new_test_that_fails_before_the_code_passes_the_check(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC, "tests/test_calc.py": OLD_TESTS})
    head = commit(root, "tests", {"tests/test_mul.py": TEST_MUL})
    assert await findings(root, base) == []
    assert git(root, "rev-parse", "HEAD") == head and git(root, "status", "--porcelain") == ""


async def test_a_new_test_that_passes_before_the_code_is_caught(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC, "tests/test_calc.py": OLD_TESTS})
    commit(root, "tests", {"tests/test_mul.py": TEST_MUL + "\n\n" + TEST_ADD_AGAIN})
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("passes_before_code", "tests/test_mul.py::test_add_again")]
    assert "passes before the layer's code is written" in found[0].why
    assert "def test_add_again" in found[0].source


async def test_a_new_module_that_cannot_import_its_feature_yet_counts_as_red_and_the_rest_still_runs(
    tmp_path: Path,
) -> None:
    """A test module that imports the name the layer adds cannot be collected before the code: that is the
    red the check wants, and every other module still runs (--continue-on-collection-errors)."""
    root, base = repo(tmp_path, {"calc.py": CALC, "tests/test_calc.py": OLD_TESTS})
    commit(
        root,
        "tests",
        {"tests/test_mul.py": "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"},
    )
    assert await findings(root, base) == []
    # the old module still ran, and still had to pass: break it, and the check says so
    commit(root, "old broken", {"calc.py": "def add(a, b):\n    return a - b\n"})
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("existing_test_fails", "tests/test_calc.py::test_add")]


async def test_an_existing_test_the_new_tests_break_is_caught(tmp_path: Path) -> None:
    root, base = repo(
        tmp_path,
        {
            "calc.py": CALC,
            "tests/test_calc.py": (
                "def test_add(base):\n    from calc import add\n\n    assert add(base, 2) == 3\n"
            ),
            "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef base():\n    return 1\n",
        },
    )
    commit(
        root,
        "tests",
        {
            "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef base():\n    return 10\n",
            "tests/test_mul.py": TEST_MUL,
        },
    )
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("existing_test_fails", "tests/test_calc.py::test_add")]
    assert "conftest.py" in found[0].why


async def test_an_existing_test_that_already_fails_on_the_base_is_not_the_new_tests_doing(
    tmp_path: Path,
) -> None:
    """AIR-9 layer 02, 2026-10-21: an existing test that opened the person's own auth database failed in the
    confined suite on the base as well, and the tester was sent back for it, then the layer blocked on it."""
    broken = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 4\n"
    root, base = repo(tmp_path, {"calc.py": CALC, "tests/test_calc.py": broken})
    commit(root, "tests", {"tests/test_mul.py": TEST_MUL})
    checks = PytestChecks(PYTEST, timeout_s=120)
    assert await checks.run(root, base) == []
    assert checks.failing_at_base == ["tests/test_calc.py::test_add"]
    assert git(root, "worktree", "list").count("\n") == 0  # the base's checkout is gone again


async def test_outcome_lines_in_a_passing_tests_captured_output_are_not_its_suites(tmp_path: Path) -> None:
    """A test that runs pytest on a fixture repository prints that run's summary, which -rA shows under the
    passing test's captured output: those lines are not the suite's outcomes (AIR-16 blocked on one)."""
    inner = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/integration/test_bad.py::test_bad - assert False\n"
        "1 failed, 1 passed in 0.01s\n"
    )
    old = OLD_TESTS + f"\n\ndef test_runs_a_fixture_suite():\n    print({inner!r})\n    assert True\n"
    root, base = repo(tmp_path, {"calc.py": CALC, "tests/test_calc.py": old})
    commit(root, "tests", {"tests/test_mul.py": TEST_MUL})
    assert await findings(root, base) == []


async def test_a_changed_test_is_held_to_the_same_red(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC, "tests/test_calc.py": OLD_TESTS})
    commit(root, "tests", {"tests/test_calc.py": OLD_TESTS + "    assert add('1', '2') == 3\n"})
    assert await findings(root, base) == []  # strings are not summed as numbers yet: red
    commit(root, "weaker", {"tests/test_calc.py": OLD_TESTS.replace("== 3", "== 3 or True")})
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("existing_test_changed", "tests/test_calc.py::test_add")]
    assert found[0].before.endswith("assert add(1, 2) == 3") and "or True" in found[0].source


SPLIT_BASE = (
    "from calc import add\n\n\ndef test_add():\n    total = add(1, 2)\n    assert total == 3\n"
    "    assert add(0, 0) == 0\n"
)
SPLIT = (
    "from calc import add\n\n\ndef test_add():\n    total = add(1, 2)\n    assert total == 3\n\n\n"
    "def test_mul():\n    from calc import mul\n\n    assert mul(2, 3) == 6\n    assert add(0, 0) == 0\n"
)
"""A new test inserted inside an existing one, as an edit anchored mid-function does: the existing test's last
line ends up under the new one (AIR-9 layer 05, 2026-10-22)."""


async def test_an_existing_test_split_by_an_insert_is_named_with_its_base_version(tmp_path: Path) -> None:
    """AIR-9 layer 05, 2026-10-22: the split test was reported as "passes before the layer's code is written",
    so the tester (and the supervisor) took it for a new test to make red, and the retry never put it back."""
    root, base = repo(tmp_path, {"calc.py": CALC, "tests/test_calc.py": SPLIT_BASE})
    commit(root, "tests", {"tests/test_calc.py": SPLIT})
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("existing_test_changed", "tests/test_calc.py::test_add")]
    [split] = found
    assert "add(0, 0)" in split.before and "add(0, 0)" not in split.source  # where it used to end
    told = feedback(found)
    assert "already there on the layer's base" in told and "assert add(0, 0) == 0" in told
    assert "passes before the layer's code" not in told
    shown = findings_text(found)
    assert shown.index("on the base:") < shown.index("add(0, 0)") < shown.index("now:")


async def test_a_layer_with_no_new_or_changed_tests_is_a_finding(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    commit(root, "tests", {"calc.py": CALC + "# a comment\n"})
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("no_new_test", "(none)")]


async def test_a_suite_that_does_not_run_its_tests_is_a_finding_with_its_output(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    commit(root, "tests", {"tests/test_mul.py": TEST_MUL})
    found = await PytestChecks(f"{PYTEST} --no-such-flag", timeout_s=120).run(root, base)
    assert [f.kind for f in found] == ["suite_did_not_run"]
    assert "no-such-flag" in found[0].why


def test_static_findings_read_the_tests_and_run_none(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    commit(
        root,
        "layer",
        {
            "tests/test_bad.py": "def test_x(:\n    pass\n",
            "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    add(1, 2)\n\n\n"
            "def test_add_again():\n    assert add(2, 2) == 4\n",
        },
    )
    kinds = sorted((f.kind, f.test) for f in static_findings(root, base))
    assert kinds == [
        ("assertion_free_test", "tests/test_calc.py::test_add"),
        ("unparseable_test", "tests/test_bad.py"),
    ]


async def test_a_test_module_that_does_not_parse_is_a_finding_with_the_error(tmp_path: Path) -> None:
    """HDS-5, 2026-10-17: the coder left `["web_search]` in its test module. The checks skipped the file and
    said passed, so the coder got no retry and the gate blocked the layer."""
    root, base = repo(tmp_path, {"calc.py": CALC})
    broken = 'from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\nX = ["a]\n'
    commit(root, "layer", {"tests/test_calc.py": broken})
    found = await findings(root, base)
    assert [(f.kind, f.test) for f in found] == [("unparseable_test", "tests/test_calc.py")]
    assert "line 8" in found[0].why and "does not parse" in found[0].why
    assert "unterminated" in found[0].why.lower() or "eol" in found[0].why.lower()


async def test_an_unparseable_module_is_reported_beside_findings_in_other_modules(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"calc.py": CALC})
    commit(
        root,
        "layer",
        {
            "tests/test_bad.py": "def test_x(:\n    pass\n",
            "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    add(1, 2)\n",
        },
    )
    kinds = sorted(f.kind for f in await findings(root, base))
    assert kinds == ["assertion_free_test", "passes_before_code", "unparseable_test"]


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
    checks = checks_for(["uv run ruff check .", "uv run pytest -q -n auto"], timeout_s=60)
    assert checks is not None and checks.step == "uv run pytest -q -n auto"  # the gate's own step, whole


def test_a_test_that_passes_before_the_code_and_matches_an_error_is_told_its_pattern_is_too_loose() -> None:
    loose = Finding(
        "passes_before_code",
        "tests/test_a.py::test_x",
        "it passes before the code",
        'def test_x():\n    with pytest.raises(E, match="a.*b"): ...',
    )
    plain = Finding(
        "passes_before_code", "tests/test_a.py::test_z", "it passes before the code", "def test_z(): ..."
    )
    text = feedback([loose, plain])
    assert text.count("`match=` pattern") == 1
    assert text.index("`match=` pattern") < text.index("test_z")  # under the test that has one


def test_feedback_names_each_test_and_why() -> None:
    text = feedback(
        [
            Finding(
                "passes_before_code",
                "tests/test_a.py::test_x",
                "it passes before the code",
                "def test_x(): ...",
            ),
            Finding(
                "assertion_free_test", "tests/test_a.py::test_y", "it has no assert", "def test_y(): ..."
            ),
        ]
    )
    assert "- tests/test_a.py::test_x: it passes before the code" in text
    assert "- tests/test_a.py::test_y: it has no assert" in text
    assert "your new tests are the only red" in text and "Do not write the feature" in text
    coder = feedback([Finding("assertion_free_test", "t::x", "it has no assert", "")], before_code=False)
    assert "Run run_tests until your tests pass" in coder and "Do not write the feature" not in coder


TODAYS_HEADER = "airlie checked the tests, and these need fixing:"
TODAYS_TESTER = (
    "Fix each one. Every test you wrote must fail now, because the code it tests is not written yet, "
    "and pass once the brief is built. A test that only checks that nothing happens (no event, no "
    "message, no change) passes now, where nothing happens either: put that check in the same test "
    "that triggers the behaviour, before the trigger, or drop it. Every test that was there before "
    "must still pass. Keep a test for each acceptance criterion. Do not write the feature. Run "
    "run_tests and check that your new tests are the only red, then stop."
)
TODAYS_CODER = (
    "Fix each one: assert the behaviour this layer adds. Keep the tests for the acceptance criteria. "
    "Run run_tests until your tests pass, then stop."
)


@pytest.mark.parametrize("before_code,what", [(True, TODAYS_TESTER), (False, TODAYS_CODER)])
@pytest.mark.parametrize(
    "found",
    [
        [],
        [
            Finding("passes_before_code", "t::a", "it passes before the code", 'raises(E, match="x")'),
            Finding("assertion_free_test", "t::b", "it has no assert", ""),
        ],
    ],
)
@pytest.mark.parametrize("advice", [None, ""])
def test_with_no_advice_feedback_is_exactly_todays_text(
    before_code: bool, what: str, found: list[Finding], advice: str | None
) -> None:
    """The supervisor's off switch: no advice, and the role reads the text it read before the supervisor."""
    listed: list[str] = []
    for f in found:
        listed.append(f"- {f.test}: {f.why}")
        if f.kind == "passes_before_code":
            listed.append("  " + hub_text("loose-match"))
    todays = "\n".join([TODAYS_HEADER, *listed, what])
    assert feedback(found, before_code=before_code, advice=advice) == todays


def test_feedback_says_what_to_do_with_a_test_that_checks_nothing_happens() -> None:
    """AIR-31 layer 02: the brief asked for a test proving the feature is a no-op below its threshold. Such a
    test passes before the code whatever the tester does, so it is told the way out, not only "fail"."""
    text = feedback(
        [Finding("passes_before_code", "tests/test_a.py::test_nothing", "it passes before the code", "...")]
    )
    assert "passes now, where nothing happens either" in text and "same test" in text and "triggers" in text


LOOSE = Finding(
    "passes_before_code", "t::a", "it passes before the code", 'with pytest.raises(E, match="x"): ...'
)
NONE_IN_IT = Finding("assertion_free_test", "t::b", "it has no assert", "")


@pytest.mark.parametrize("advice", [None, "Read the fixtures first."])
def test_feedback_said_is_the_text_and_the_hub_ids_in_it_in_order(advice: str | None) -> None:
    """The pipeline emits a `steer` event per id when it sends the text, so the ids are exactly the hub texts
    in it, a loose `match=` hint once per finding that has one."""
    tester = feedback_said([LOOSE, NONE_IN_IT, LOOSE], advice=advice)
    assert tester == (
        feedback([LOOSE, NONE_IN_IT, LOOSE], advice=advice),
        ("checks-found", "loose-match", "loose-match", "checks-fix-before-code"),
    )
    coder = feedback_said([NONE_IN_IT], before_code=False, advice=advice)
    assert coder == (
        feedback([NONE_IN_IT], before_code=False, advice=advice),
        ("checks-found", "checks-fix-with-code"),
    )
    assert tester[0].startswith(hub_text("checks-found") + "\n- t::a: ")
    assert tester[0].endswith("\n" + hub_text("checks-fix-before-code"))
    assert coder[0].endswith("\n" + hub_text("checks-fix-with-code"))


def test_amend_feedback_said_is_the_text_and_the_amend_id() -> None:
    failing = ["tests/test_a.py::test_x"]
    said = amend_feedback_said(failing, "I built it.", ("the empty file passes",))
    assert said == (amend_feedback(failing, "I built it.", ("the empty file passes",)), ("amend",))
    assert said[0].startswith(hub_text("amend") + "\n\nFailing:\n- tests/test_a.py::test_x")


async def test_run_one_runs_only_the_named_test(tmp_path: Path) -> None:
    root, _ = repo(
        tmp_path,
        {"tests/test_a.py": "def test_good():\n    assert True\n\n\ndef test_bad():\n    assert missing\n"},
    )
    out = await PytestChecks(PYTEST, timeout_s=120).run_one(root, "tests/test_a.py::test_bad")
    assert "NameError" in out and "test_good" not in out


def test_is_testers_test() -> None:
    by = {"tests/test_p.py": {"test_new", "TestX"}}
    assert is_testers_test("tests/test_w.py::test_a", {"tests/test_w.py"}, by)
    assert is_testers_test("tests/test_p.py::test_new", set(), by)
    assert not is_testers_test("tests/test_p.py::test_old", set(), by)
    assert is_testers_test("tests/test_p.py::TestX::test_a[1]", set(), by)
    assert is_testers_test("tests/test_p.py::test_new[a-b]", set(), by)
    assert not is_testers_test("tests/test_p.py", set(), by)
