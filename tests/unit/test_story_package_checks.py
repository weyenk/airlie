"""The readiness check on a layer's package (P1, 2026-10-24): the package's tests are placed in a scratch
worktree at the layer's base and checked with no model: files, lint with the fixer, parse, covers, imports and
red on the base. Real git repositories, with pytest and ruff as the gate."""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from airlie.prompts import text as hub_text
from airlie.story.layer_checks import Finding
from airlie.story.package import Interface, Package, PackageFile, joined_tests
from airlie.story.package_checks import (
    PackageChecker,
    as_checked,
    imports_unresolved,
    package_feedback_said,
    place_package,
)
from tests.unit.git_helpers import git
from tests.unit.test_story_layer_checks import CALC, OLD_TESTS, repo

PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q"
RUFF = f"{shlex.quote(sys.executable)} -m ruff"
RUFF_GATE = (f"{RUFF} check .", f"{RUFF} format --check .")
RUFF_TOML = 'line-length = 60\n\n[lint]\nselect = ["E", "F", "I"]\n'
TEST_MUL = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
MUL = Interface("calc.py", "def mul(a, b) -> int", "the product")


def package(
    tests: dict[str, str] | None = None,
    covers: dict[str, tuple[str, ...]] | None = None,
    files: tuple[PackageFile, ...] = (PackageFile("calc.py", "modify"),),
    interfaces: tuple[Interface, ...] = (MUL,),
) -> Package:
    tests = {"tests/test_mul.py": TEST_MUL} if tests is None else tests
    covers = {"AC1": ("tests/test_mul.py::test_mul",)} if covers is None else covers
    return Package("mul", tests, files, interfaces, ("add mul to calc.py",), covers, "the tests pass")


def calc(tmp_path: Path, extra: dict[str, str] | None = None) -> tuple[Path, str]:
    return repo(tmp_path, {"calc.py": CALC, "tests/test_base.py": OLD_TESTS, **(extra or {})})


def worktrees(root: Path) -> list[str]:
    return [line for line in git(root, "worktree", "list").splitlines() if line]


async def check(tmp_path: Path, root: Path, base: str, p: Package, gate: tuple[str, ...] = (PYTEST,)):
    return await PackageChecker(gate, timeout_s=120).check(p, root, base, tmp_path / "scratch" / "package-01")


async def test_a_red_package_against_a_pytest_gate_has_no_findings_and_leaves_no_worktree(
    tmp_path: Path,
) -> None:
    root, base = calc(tmp_path)
    got, found = await check(tmp_path, root, base, package())
    assert found == []
    assert got.tests == package().tests
    assert len(worktrees(root)) == 1
    assert not (tmp_path / "scratch" / "package-01").exists()
    assert not (root / "tests" / "test_mul.py").exists()  # the real tree is untouched


async def test_a_test_that_passes_before_the_code_is_a_finding(tmp_path: Path) -> None:
    root, base = calc(tmp_path)
    tests = {
        "tests/test_add.py": "from calc import add\n\n\ndef test_add_two():\n    assert add(2, 2) == 4\n"
    }
    _, found = await check(
        tmp_path, root, base, package(tests, {"AC1": ("tests/test_add.py::test_add_two",)})
    )
    assert [(f.kind, f.test) for f in found] == [("passes_before_code", "tests/test_add.py::test_add_two")]


async def test_a_regression_guard_may_pass_before_the_code_and_every_other_test_must_still_fail(
    tmp_path: Path,
) -> None:
    """AIR-61 layer 02 (2026-11-05): a criterion already true on main has a test that can never be red; the
    package marks it a guard, and the red check lets it pass on the base. A test that is no guard and passes
    there is still a finding."""
    root, base = calc(tmp_path)
    add_tests = (
        "from calc import add\n\n\ndef test_add_two():\n    assert add(2, 2) == 4\n\n\n"
        "def test_add_zero():\n    assert add(0, 2) == 2\n"
    )
    tests = {"tests/test_add.py": add_tests, "tests/test_mul.py": TEST_MUL}
    covers = {
        "AC1": ("tests/test_mul.py::test_mul", "tests/test_add.py::test_add_two"),
        "AC2": ("tests/test_add.py::test_add_zero",),
    }
    guarded = replace(package(tests, covers), guards={"tests/test_add.py::test_add_two": "add is on main"})
    _, found = await check(tmp_path, root, base, guarded)
    assert [(f.kind, f.test) for f in found] == [("passes_before_code", "tests/test_add.py::test_add_zero")]


async def test_a_covered_test_id_that_is_not_in_the_file_is_a_finding(tmp_path: Path) -> None:
    root, base = calc(tmp_path)
    tests = {"tests/test_base.py": OLD_TESTS + "\n\n" + TEST_MUL.split("\n\n\n", 1)[1]}
    covers = {"AC1": ("tests/test_base.py::test_add", "tests/test_base.py::test_mul")}
    _, found = await check(tmp_path, root, base, package(tests, covers))
    assert [(f.kind, f.test) for f in found] == [("covers_missing", "tests/test_base.py::test_add")]
    assert "new or changed" in found[0].why


async def test_an_existing_test_the_agreed_proof_names_is_covered_as_a_regression_guard(
    tmp_path: Path,
) -> None:
    """AIR-62 layer 01 (2026-11-06): the agreed proof named existing tests of the file the layer adds to as
    the proof of its criteria that already hold. Covered, they were `covers_missing`; left out,
    `proof_mismatch`: no package could pass. An existing test the proof names, or one the package declares a
    guard, is covered as a regression guard; one that is neither is still `covers_missing`."""
    root, base = calc(tmp_path)
    tests = {"tests/test_base.py": OLD_TESTS + "\n\n" + TEST_MUL.split("\n\n\n", 1)[1]}
    covers = {"AC1": ("tests/test_base.py::test_mul",), "AC2": ("tests/test_base.py::test_add",)}
    checker = PackageChecker((PYTEST,), timeout_s=120)
    scratch = tmp_path / "scratch" / "package-01"
    agreed = {"tests/test_base.py::test_mul", "tests/test_base.py::test_add"}
    got, found = await checker.check(package(tests, covers), root, base, scratch, proof_ids=agreed)
    assert found == []
    assert got.guards == {"tests/test_base.py::test_add": "existing test the agreed proof names"}
    assert got.is_guard("tests/test_base.py::test_add") and not got.is_guard("tests/test_base.py::test_mul")
    # declared a guard, it needs no proof, and its own reason is kept
    guarded = replace(package(tests, covers), guards={"tests/test_base.py::test_add": "add is on main"})
    got, found = await checker.check(guarded, root, base, scratch)
    assert found == []
    assert got.guards == {"tests/test_base.py::test_add": "add is on main"}
    # neither named by the proof nor a guard: still not this layer's test
    got, found = await checker.check(
        package(tests, covers), root, base, scratch, proof_ids={"tests/test_base.py::test_mul"}
    )
    assert [(f.kind, f.test) for f in found] == [("covers_missing", "tests/test_base.py::test_add")]
    assert got.guards == {}


async def test_an_import_of_a_name_the_package_neither_defines_nor_declares_is_a_finding(
    tmp_path: Path,
) -> None:
    root, base = calc(tmp_path, {"pkg/__init__.py": ""})
    text = (
        "from calc import divide, mul\nimport pkg.missing\n\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n    assert divide(6, 3) == 2\n"
    )
    _, found = await check(tmp_path, root, base, package({"tests/test_mul.py": text}))
    assert sorted((f.kind, f.test) for f in found) == [
        ("import_unresolved", "tests/test_mul.py: calc.divide"),
        ("import_unresolved", "tests/test_mul.py: pkg.missing"),
    ]
    by_test = {f.test: f.why for f in found}
    assert (
        "defined neither in the file nor in the package's interfaces"
        in by_test["tests/test_mul.py: calc.divide"]
    )
    assert "package creates no such file" in by_test["tests/test_mul.py: pkg.missing"]


async def test_an_import_of_a_name_declared_in_the_packages_interfaces_for_a_created_file_resolves(
    tmp_path: Path,
) -> None:
    root, base = calc(tmp_path, {"src/__init__.py": ""})
    text = "from store import Store, store\n\n\ndef test_it():\n    assert store() == 1\n"
    interfaces = (
        Interface("src/store.py", "def store() -> int"),
        Interface("src/store.py", "class Store"),
    )
    files = (PackageFile("src/store.py", "create"),)
    _, found = await check(
        tmp_path,
        root,
        base,
        package({"tests/test_store.py": text}, {"AC1": ("tests/test_store.py::test_it",)}, files, interfaces),
    )
    assert found == []


async def test_a_file_the_packages_interfaces_name_but_its_files_leave_out_is_one_it_creates(
    tmp_path: Path,
) -> None:
    """Roman bundle 2 (2026-10-29): interfaces for sandbox/roman.py, files only the test; the import
    of sandbox.roman was refused twice and the story blocked."""
    root, base = calc(tmp_path, {"src/__init__.py": "", "src/old.py": "x = 1\n"})
    text = "from store import store\n\n\ndef test_it():\n    assert store() == 1\n"
    interfaces = (Interface("src/store.py", "def store() -> int"), Interface("src/old.py", "def y() -> int"))
    got, found = await check(
        tmp_path,
        root,
        base,
        package({"tests/test_store.py": text}, {"AC1": ("tests/test_store.py::test_it",)}, (), interfaces),
    )
    assert found == []
    assert [(f.path, f.action) for f in got.files] == [("src/store.py", "create")]  # src/old.py is there


async def test_a_test_file_written_in_full_and_called_a_change_is_created_when_the_base_lacks_it(
    tmp_path: Path,
) -> None:
    """Roman, 2026-10-30: a re-plan after a red build named the thrown-away build's test file "modify", and
    the package came back file_missing. The package writes it in full, so it creates it."""
    root, base = calc(tmp_path)
    p = package()
    p = replace(p, files=(*p.files, PackageFile("tests/test_mul.py", "modify")))
    got, found = await check(tmp_path, root, base, p)
    assert [f.kind for f in found] == []
    assert ("tests/test_mul.py", "create") in [(f.path, f.action) for f in got.files]


UNKNOWN_METHOD_TESTS = """import unittest

from calc import mul


def _assert_six(value):
    assert value == 6


class Base(unittest.TestCase):
    def _assert_mul(self, a, b, want):
        self.assertEqual(mul(a, b), want)


class MulTest(Base):
    def setUp(self):
        self.check = self._assert_mul

    def test_mul(self):
        self._assert_mul(2, 3, 6)
        self.check(1, 1, 1)
        self.assertTrue(mul(1, 0) == 0)

    def test_mul_six(self):
        self._assert_six(mul(2, 3))
"""


async def test_a_test_that_calls_a_method_its_class_does_not_have_is_a_finding(tmp_path: Path) -> None:
    """2026-10-29, bundle 3 (roman): `_assert_malformed` written at the top level, called as a method.
    What the class, its base in the file, setUp's assignments and unittest.TestCase have are not findings."""
    root, base = calc(tmp_path)
    covers = {"AC1": ("tests/test_mul.py::MulTest::test_mul", "tests/test_mul.py::MulTest::test_mul_six")}
    _, found = await check(tmp_path, root, base, package({"tests/test_mul.py": UNKNOWN_METHOD_TESTS}, covers))
    unknown = [f for f in found if f.kind == "unknown_method"]
    assert [(f.test, f.why.split(",")[0]) for f in unknown] == [
        ("tests/test_mul.py::MulTest::test_mul_six", "it calls self._assert_six()")
    ]


ASSIGNED_CALLABLE_TESTS = """import unittest
from collections.abc import Callable

from calc import mul


class RecordingBoard:
    def __init__(self):
        self.moves = []


class FlakyBoard(RecordingBoard):
    def __init__(self, refuses: Callable[[str, str], bool], message: str = "refused") -> None:
        super().__init__()
        self.refuses, self.message = refuses, message

    def move(self, card, column):
        if self.refuses("move", column):
            raise RuntimeError(self.message)


class MulTest(unittest.TestCase):
    def setUp(self):
        self.double, [self.triple] = (lambda n: mul(n, 2)), [lambda n: mul(n, 3)]
        self.half: Callable[[int], int] = lambda n: n // 2

    def test_mul(self):
        self.assertEqual(self.double(3), 6)
        self.assertEqual(self.triple(2), 6)
        self.assertEqual(self.half(12), 6)
"""


async def test_a_callable_kept_on_the_instance_or_a_fake_that_is_no_test_case_is_no_finding(
    tmp_path: Path,
) -> None:
    """AIR-47 layer 02, 2026-11-01: test_story_pipeline.py's FlakyBoard keeps its `refuses` callable with
    `self.refuses, self.message = refuses, message`, and the package came back unknown_method twice, which
    blocked the story. A name assigned through a tuple, a list or an annotation is the class's own; a fake
    whose bases never reach unittest.TestCase is no test class to check."""
    root, base = calc(tmp_path)
    covers = {"AC1": ("tests/test_mul.py::MulTest::test_mul",)}
    _, found = await check(
        tmp_path, root, base, package({"tests/test_mul.py": ASSIGNED_CALLABLE_TESTS}, covers)
    )
    assert [f.test for f in found if f.kind == "unknown_method"] == []


SWEEP_TESTS = """import itertools
import unittest

from calc import mul

LETTERS = "IVXLCDM"


class MulTest(unittest.TestCase):
    def test_short_sweep(self):
        for length in range(1, 5):
            for combo in itertools.product(LETTERS, repeat=length):
                self.assertTrue(mul(1, len(combo)) >= 1)

    def test_long_sweep(self):
        for combo in itertools.product(LETTERS, repeat=16):
            self.assertTrue(mul(1, len(combo)) == 16)

    def test_sliced_sweep(self):
        for combo in itertools.islice(itertools.product(LETTERS, repeat=15), 1000):
            self.assertTrue(mul(1, 1) == 1)

    def test_unreadable_sweep(self):
        for combo in itertools.product(make_letters(), repeat=16):
            self.assertTrue(mul(1, 1) == 1)
"""


async def test_a_test_that_sweeps_more_cases_than_can_run_is_a_finding(tmp_path: Path) -> None:
    """2026-10-29, bundle 3 (roman): itertools.product(letters, repeat=16) timed the coder's runs out."""
    root, base = calc(tmp_path)
    ids = ("test_short_sweep", "test_long_sweep", "test_sliced_sweep", "test_unreadable_sweep")
    covers = {"AC1": tuple(f"tests/test_mul.py::MulTest::{t}" for t in ids)}
    _, found = await check(tmp_path, root, base, package({"tests/test_mul.py": SWEEP_TESTS}, covers))
    [big] = [f for f in found if f.kind == "test_too_large"]
    assert big.test == "tests/test_mul.py::MulTest::test_long_sweep"
    assert "33,232,930,569,601 cases (7 to the power 16)" in big.why


async def test_third_party_imports_are_not_checked(tmp_path: Path) -> None:
    root, base = calc(tmp_path)
    text = "import pytest\nfrom httpx import Client\nfrom calc import mul\n\n\ndef test_x():\n    pass\n"
    p = package({"tests/test_x.py": text})
    place_package(root, p)
    try:
        assert imports_unresolved(root, p) == []
    finally:
        git(root, "clean", "-fdq")


async def test_what_ruff_fixes_is_carried_into_the_package(tmp_path: Path) -> None:
    root, base = calc(tmp_path, {"ruff.toml": RUFF_TOML})
    unsorted = "from calc import mul\nimport os\n\n\ndef test_mul():\n    assert mul(2, os.sep) == 6\n"
    sorted_ = "import os\n\nfrom calc import mul\n\n\ndef test_mul():\n    assert mul(2, os.sep) == 6\n"
    got, found = await check(
        tmp_path, root, base, package({"tests/test_mul.py": unsorted}), (*RUFF_GATE, PYTEST)
    )
    assert found == []
    assert got.tests["tests/test_mul.py"] == sorted_


async def test_what_ruff_cannot_fix_is_a_lint_finding_with_the_rule(tmp_path: Path) -> None:
    root, base = calc(tmp_path, {"ruff.toml": RUFF_TOML})
    long = TEST_MUL + '\n\nNOTE = "a string that is much too long for a line of sixty characters"\n'
    _, found = await check(tmp_path, root, base, package({"tests/test_mul.py": long}), (*RUFF_GATE, PYTEST))
    assert [f.kind for f in found] == ["lint_fails"]
    assert "E501" in found[0].why


async def test_a_created_file_that_exists_and_a_modified_file_that_does_not_are_findings(
    tmp_path: Path,
) -> None:
    root, base = calc(tmp_path)
    files = (PackageFile("calc.py", "create"), PackageFile("src/new.py", "modify"))
    _, found = await check(tmp_path, root, base, package(files=files))
    assert sorted((f.kind, f.test) for f in found) == [
        ("file_exists", "calc.py"),
        ("file_missing", "src/new.py"),
    ]


async def test_a_gate_without_pytest_skips_the_red_check_and_still_parses_and_lints(tmp_path: Path) -> None:
    root, base = calc(tmp_path, {"ruff.toml": RUFF_TOML})
    checker = PackageChecker(RUFF_GATE, timeout_s=120)
    assert not checker.runs_red
    scratch = tmp_path / "scratch" / "package-01"
    # the test passes before the code (add), but no pytest runs, so it is not a finding
    green = {
        "tests/test_add.py": "from calc import add\n\n\ndef test_add_two():\n    assert add(2, 2) == 4\n"
    }
    _, found = await checker.check(
        package(green, {"AC1": ("tests/test_add.py::test_add_two",)}), root, base, scratch
    )
    assert found == []
    _, found = await checker.check(package({"tests/test_mul.py": "def test_x(:\n"}, {}), root, base, scratch)
    assert "unparseable_test" in [f.kind for f in found]


def test_place_package_writes_only_the_test_files_and_returns_their_paths(tmp_path: Path) -> None:
    p = package({"tests/unit/test_b.py": "x = 1\n", "tests/test_a.py": "y = 2\n"})
    assert place_package(tmp_path, p) == ["tests/test_a.py", "tests/unit/test_b.py"]
    assert sorted(f.relative_to(tmp_path).as_posix() for f in tmp_path.rglob("*") if f.is_file()) == [
        "tests/test_a.py",
        "tests/unit/test_b.py",
    ]


def test_place_package_refuses_a_path_outside_the_worktree(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    for bad in ("../outside_test.py", "/etc/test_x.py"):
        with pytest.raises(ValueError, match="inside"):
            place_package(work, package({bad: "x = 1\n"}))
    assert not (tmp_path / "outside_test.py").exists()


def test_package_feedback_names_each_finding_and_ends_with_package_fix() -> None:
    found = [
        Finding("passes_before_code", "tests/test_a.py::test_a", "it passes", "def test_a(): ...", ""),
        Finding("import_unresolved", "tests/test_a.py: calc.divide", "not defined", "", ""),
    ]
    told, ids = package_feedback_said(found)
    assert ids == ("checks-found", "passes-before-code-choice", "package-fix")
    assert "- tests/test_a.py::test_a: it passes" in told
    assert "- tests/test_a.py: calc.divide: not defined" in told
    assert told.endswith(hub_text("package-fix"))


async def test_a_package_that_misses_an_agreed_test_is_a_proof_mismatch_and_one_it_adds_is_not(
    tmp_path: Path,
) -> None:
    """D9, D12: a package covers every agreed test of its layer, by id; a test it adds is kept and reported
    (`beyond_proof`), since sending it back cost ~11 minutes a time (roman, 2026-10-27)."""
    from airlie.story.package_checks import beyond_proof

    root, base = calc(tmp_path)
    agreed = {"tests/test_mul.py::test_mul"}
    checker = PackageChecker((PYTEST,), timeout_s=120)
    scratch = tmp_path / "scratch" / "package-01"
    _, found = await checker.check(package(), root, base, scratch, proof_ids=agreed)
    assert found == []
    _, found = await checker.check(
        package(), root, base, scratch, proof_ids=agreed | {"tests/test_mul.py::test_mul_zero"}
    )
    assert [(f.kind, f.test) for f in found] == [("proof_mismatch", "tests/test_mul.py::test_mul_zero")]
    assert "agreed proof names it, but your package does not cover it" in found[0].why
    _, found = await checker.check(
        package(), root, base, scratch, proof_ids={"tests/test_mul.py::test_other"}
    )
    assert [(f.kind, f.test) for f in found] == [("proof_mismatch", "tests/test_mul.py::test_other")]
    assert beyond_proof(package(), {"tests/test_mul.py::test_other"}) == ["tests/test_mul.py::test_mul"]
    assert beyond_proof(package(), agreed) == []
    # a parametrized test is its function's id, and with no proof there is nothing to match
    param = package(covers={"AC1": ("tests/test_mul.py::test_mul[2-3]",)})
    _, found = await checker.check(param, root, base, scratch, proof_ids=agreed)
    assert [f.kind for f in found if f.kind == "proof_mismatch"] == []
    _, found = await checker.check(package(), root, base, scratch)
    assert found == []


def test_an_agreed_id_without_its_class_is_the_package_test_it_names() -> None:
    """The meeting writes `file::name`; a unittest package stores the full `file::Class::name`."""
    from airlie.story.package_checks import proof_mismatches

    p = package(covers={"AC1": ("tests/test_mul.py::MulTest::test_mul",)})
    assert proof_mismatches(p, {"tests/test_mul.py::test_mul"}) == []
    assert proof_mismatches(p, {"tests/test_mul.py::MulTest::test_mul"}) == []
    assert [f.test for f in proof_mismatches(p, {"tests/test_other.py::test_mul"})] == [
        "tests/test_other.py::test_mul",  # agreed and not covered; the covered one is beyond the proof
    ]


def test_an_agreed_vitest_id_without_its_describe_is_the_package_test_it_names() -> None:
    """Wiki run B (2026-10-27): the proof agreed `TopBar.test.ts::the tab bar has a Wiki link`, and the
    package's file names it `TopBar > the tab bar has a Wiki link`, as Vitest does."""
    from airlie.story.package_checks import proof_mismatches

    full = "web/src/TopBar.test.ts::TopBar > the tab bar has a Wiki link"
    p = package(covers={"AC1": (full,)})
    assert proof_mismatches(p, {"web/src/TopBar.test.ts::the tab bar has a Wiki link"}) == []
    assert proof_mismatches(p, {full}) == []
    assert [f.test for f in proof_mismatches(p, {"web/src/TopBar.test.ts::a Wiki link"})] == [
        "web/src/TopBar.test.ts::a Wiki link",  # only a whole title after the describe is the same test
    ]


BASE_TESTS = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
ADDED = "import os\n\nfrom calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6 and os.sep\n"


def test_the_package_as_checked_holds_its_test_files_and_only_the_additions_to_an_existing_one() -> None:
    """AIR-59, 2026-11-05: a retry on a conversation started fresh did not hold the package it was to fix
    ("I don't have the exact text of the middle tests!") and the architect wrote it again from memory, with
    the same 111-character line; AIR-61 changed a test's meaning doing so."""
    whole = joined_tests(BASE_TESTS, ADDED)
    p = replace(
        package({"tests/test_base.py": whole, "tests/test_new.py": TEST_MUL}),
        added={"tests/test_base.py": ADDED},
    )
    text = as_checked(p, {"tests/test_base.py": BASE_TESTS}.get)
    assert "### test file: tests/test_new.py\n```python\n" + TEST_MUL + "```" in text
    additions = text.split("### test additions: tests/test_base.py\n")[1].split("```")[1]
    assert "def test_mul():" in additions and "import os" in additions and "from calc import mul" in additions
    assert "def test_add():" not in additions  # the base's own tests are not copied out
    obj = json.loads(text.split("```json\n")[1].split("```")[0])
    assert obj["type"] == "package" and obj["covers"] == {"AC1": ["tests/test_mul.py::test_mul"]}
    assert obj["steps"] == ["add mul to calc.py"] and obj["files"][0]["path"] == "calc.py"
    assert "guards" not in obj  # none to carry
    guarded = replace(p, guards={"tests/test_mul.py::test_mul": "mul is on main"})
    obj = json.loads(
        as_checked(guarded, {"tests/test_base.py": BASE_TESTS}.get).split("```json\n")[1].split("```")[0]
    )
    assert obj["guards"] == {"tests/test_mul.py::test_mul": "mul is on main"}  # a retry keeps its guards


def test_a_package_too_long_to_carry_says_which_test_file_it_leaves_out() -> None:
    big = "def test_a():\n    assert True\n" + "# filler\n" * 2000
    p = package({"tests/test_big.py": big, "tests/test_mul.py": TEST_MUL})
    text = as_checked(p, lambda _: None, 5_000)
    assert "### test file: tests/test_mul.py" in text and "# filler" not in text
    assert "tests/test_big.py" in text and "left out" in text
    assert "```json" in text


def test_package_feedback_with_the_package_as_checked_puts_it_before_package_fix() -> None:
    found = [Finding("lint_fails", "ruff check .", "E501 Line too long (111 > 110)", "", "")]
    told, ids = package_feedback_said(found, previous="### test file: tests/test_a.py\n```python\nx\n```")
    assert ids == ("checks-found", "package-as-checked", "package-fix")
    assert told.index("E501") < told.index(hub_text("package-as-checked")) < told.index("### test file")
    assert told.endswith(hub_text("package-fix"))
