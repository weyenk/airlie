"""The readiness check on a layer's package (P1, 2026-10-24): the package's tests are placed in a scratch
worktree at the layer's base and checked with no model: files, lint with the fixer, parse, covers, imports and
red on the base. Real git repositories, with pytest and ruff as the gate."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from airlie.prompts import text as hub_text
from airlie.story.layer_checks import Finding
from airlie.story.package import Interface, Package, PackageFile
from airlie.story.package_checks import (
    PackageChecker,
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


async def test_a_covered_test_id_that_is_not_in_the_file_is_a_finding(tmp_path: Path) -> None:
    root, base = calc(tmp_path)
    tests = {"tests/test_base.py": OLD_TESTS + "\n\n" + TEST_MUL.split("\n\n\n", 1)[1]}
    covers = {"AC1": ("tests/test_base.py::test_add", "tests/test_base.py::test_mul")}
    _, found = await check(tmp_path, root, base, package(tests, covers))
    assert [(f.kind, f.test) for f in found] == [("covers_missing", "tests/test_base.py::test_add")]
    assert "new or changed" in found[0].why


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
    long = (
        TEST_MUL + "\n\n# a comment that is much too long for a line of sixty characters, and ruff keeps it\n"
    )
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


async def test_a_package_that_misses_an_agreed_test_or_adds_one_is_a_proof_mismatch(tmp_path: Path) -> None:
    """D9, D12: a package covers exactly the agreed tests of its layer, by id; none left out, none added."""
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
    assert sorted((f.kind, f.test) for f in found) == [
        ("proof_mismatch", "tests/test_mul.py::test_mul"),
        ("proof_mismatch", "tests/test_mul.py::test_other"),
    ]
    assert any("your package covers it, but the agreed proof does not name it" in f.why for f in found)
    # a parametrized test is its function's id, and with no proof there is nothing to match
    param = package(covers={"AC1": ("tests/test_mul.py::test_mul[2-3]",)})
    _, found = await checker.check(param, root, base, scratch, proof_ids=agreed)
    assert [f.kind for f in found if f.kind == "proof_mismatch"] == []
    _, found = await checker.check(package(), root, base, scratch)
    assert found == []
