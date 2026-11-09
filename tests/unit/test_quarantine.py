"""The quarantine list (tests/unit/quarantine.toml) and the test budget (AIRLIE_TEST_BUDGET_MS)."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.conftest import BUDGET, ROOT, Budget, is_quarantined, load_quarantine, over_budget


def test_every_entry_names_a_test_that_exists_and_says_why() -> None:
    """A renamed or deleted test leaves no stale line behind, and every reason is tagged slow: or clock:."""
    entries = load_quarantine()
    trees: dict[str, set[str]] = {}
    for entry in entries:
        file, _, name = entry.split("[")[0].partition("::")
        assert (ROOT / file).is_file(), entry
        if not name:
            continue
        if file not in trees:
            tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
            found = set()
            for node in tree.body:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    found.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    subs = [s for s in node.body if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef)]
                    found |= {f"{node.name}::{s.name}" for s in subs}
            trees[file] = found
        assert name in trees[file], entry


def test_a_reason_without_its_tag_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "q.toml"
    bad.write_text('[quarantine]\n"tests/unit/test_x.py::test_y" = "too slow"\n')
    with pytest.raises(pytest.UsageError, match="slow: or clock:"):
        load_quarantine(bad)


def test_an_entry_covers_every_case_of_its_test_and_a_file_entry_every_test_in_it() -> None:
    entries = {"tests/unit/test_a.py::test_b": "slow: 600 ms", "tests/unit/test_c.py": "clock: sleeps"}
    assert is_quarantined("tests/unit/test_a.py::test_b[one]", entries)
    assert is_quarantined("tests/unit/test_c.py::TestX::test_y", entries)
    assert not is_quarantined("tests/unit/test_a.py::test_bb", entries)
    one_case = {"tests/unit/test_a.py::test_b[slow]": "slow: 700 ms"}
    assert is_quarantined("tests/unit/test_a.py::test_b[slow]", one_case)
    assert not is_quarantined("tests/unit/test_a.py::test_b[fast]", one_case)


def test_the_budget_names_each_test_over_it_that_is_not_quarantined_slowest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = next(iter(load_quarantine()))
    budget = Budget()
    for nodeid, seconds in [("tests/unit/test_a.py::fast", 0.1), ("tests/unit/test_a.py::slow", 0.7),
                            ("tests/unit/test_a.py::slower[x]", 0.9), (f"{listed}[p]", 5.0)]:  # fmt: skip
        budget.pytest_runtest_logreport(SimpleNamespace(nodeid=nodeid, duration=seconds / 3))  # type: ignore[arg-type]
        budget.pytest_runtest_logreport(SimpleNamespace(nodeid=nodeid, duration=seconds * 2 / 3))  # type: ignore[arg-type]
    config = SimpleNamespace(stash={BUDGET: budget})
    monkeypatch.setenv("AIRLIE_TEST_BUDGET_MS", "500")
    named = [n for n, _ in over_budget(config)]  # type: ignore[arg-type]
    assert named == ["tests/unit/test_a.py::slower[x]", "tests/unit/test_a.py::slow"]
    assert over_budget(SimpleNamespace(stash={})) == []  # type: ignore[arg-type]  # no budget set: nothing
