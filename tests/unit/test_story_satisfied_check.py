"""A satisfied reply is held to its evidence (review of PR #244, 2026-11-06): each test it names runs on the
layer's base, confined as the red check runs, and must exist and pass there; each layer number must be one
built below. Otherwise it is a `satisfied_unproven` finding for the architect."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from airlie.story.layer_checks import checks_for
from airlie.story.package import Satisfied
from airlie.story.satisfied_check import unproven
from tests.unit.git_helpers import git
from tests.unit.test_story_layer_checks import CALC, OLD_TESTS, repo

PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q"
FAILING = "from calc import add\n\n\ndef test_add_wrong():\n    assert add(1, 2) == 4\n"


async def check(tmp_path: Path, by: tuple[str, ...], built: tuple[str, ...] = (), gate: str = PYTEST):
    root, base = repo(
        tmp_path, {"calc.py": CALC, "tests/test_base.py": OLD_TESTS, "tests/test_bad.py": FAILING}
    )
    found = await unproven(
        Satisfied(by, "add already adds"),
        built=built,
        repo=root,
        base=base,
        scratch=tmp_path / "scratch" / "satisfied-02",
        checks=checks_for([gate], timeout_s=120),
        node_cache=tmp_path / "node_modules",
        timeout_s=120,
    )
    return root, found


async def test_a_satisfied_reply_whose_tests_pass_on_the_base_and_whose_layers_were_built_holds(
    tmp_path: Path,
) -> None:
    root, found = await check(tmp_path, ("01", "tests/test_base.py::test_add"), built=("01",))
    assert found == []
    assert git(root, "worktree", "list").count("\n") == 0  # the scratch checkout is removed


async def test_a_named_test_that_fails_or_is_not_there_on_the_base_is_a_finding(tmp_path: Path) -> None:
    _, found = await check(tmp_path, ("tests/test_bad.py::test_add_wrong", "tests/test_base.py::test_gone"))
    assert [(f.kind, f.test) for f in found] == [
        ("satisfied_unproven", "tests/test_bad.py::test_add_wrong"),
        ("satisfied_unproven", "tests/test_base.py::test_gone"),
    ]
    assert found[0].why.startswith("satisfied, but it fails on the base")
    assert found[1].why.startswith("satisfied, but it does not exist on the base")


async def test_a_layer_number_that_was_not_built_below_is_a_finding(tmp_path: Path) -> None:
    _, found = await check(tmp_path, ("01",), built=())
    assert [(f.kind, f.test) for f in found] == [("satisfied_unproven", "01")]
    assert "layer 01 was not built below this one" in found[0].why


async def test_a_pytest_test_the_gate_cannot_run_is_a_finding(tmp_path: Path) -> None:
    _, found = await check(tmp_path, ("tests/test_base.py::test_add",), gate="./check.sh")
    assert [f.kind for f in found] == ["satisfied_unproven"]
    assert "the gate runs no pytest" in found[0].why
