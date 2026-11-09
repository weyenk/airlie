"""The quarantine list (tests/unit/quarantine.toml) and the test budget (AIRLIE_TEST_BUDGET_MS)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.budget import CHARGE, FIRST, Budget
from tests.unit.conftest import is_quarantined, load_quarantine, stale_entries


def test_an_entry_no_collected_test_matches_is_stale_parametrized_cases_exactly(tmp_path: Path) -> None:
    """The conftest fails a whole tests/unit run on these: a renamed or deleted test leaves no line behind."""
    for name in ("test_a.py", "test_b.py", "test_mac.py"):
        (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
        (tmp_path / "tests" / "unit" / name).write_text("")
    collected = [
        "tests/unit/test_a.py::test_x[one]",
        "tests/unit/test_a.py::TestK::test_y",
        "tests/unit/test_b.py::t",
    ]
    entries = {
        "tests/unit/test_a.py::test_x": "slow: 1 ms",  # every case: matches
        "tests/unit/test_a.py::test_x[one]": "slow: 1 ms",  # the exact case: matches
        "tests/unit/test_a.py::test_x[two]": "slow: 1 ms",  # a case that is gone
        "tests/unit/test_a.py::test_x[on": "slow: 1 ms",  # no prefix matching
        "tests/unit/test_a.py::TestK::test_y": "slow: 1 ms",
        "tests/unit/test_a.py::test_gone": "slow: 1 ms",
        "tests/unit/test_b.py": "slow: 1 ms",  # a whole file with tests in it
        "tests/unit/test_moved.py::t": "slow: 1 ms",  # a file that is not there
        "tests/unit/test_mac.py::t": "slow: 1 ms",  # there but not collected here (MAC_ONLY, --ignore)
    }
    assert stale_entries(entries, collected, tmp_path) == [
        "tests/unit/test_a.py::test_x[two]",
        "tests/unit/test_a.py::test_x[on",
        "tests/unit/test_a.py::test_gone",
        "tests/unit/test_moved.py::t",
    ]


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


def test_the_budget_names_each_test_over_it_twice_that_is_not_quarantined_costliest_first() -> None:
    entries = load_quarantine()
    listed = next(iter(entries))
    budget = Budget(0.5, lambda nodeid: is_quarantined(nodeid, entries))
    rows = [("tests/unit/test_a.py::cheap", None, 0.1), ("tests/unit/test_a.py::once", 0.7, 0.3),
            ("tests/unit/test_a.py::twice", 0.7, 0.6), ("tests/unit/test_a.py::twice_more[x]", 0.9, 0.8),
            (f"{listed}[p]", None, 5.0)]  # fmt: skip
    for nodeid, first, charge in rows:
        props = [[CHARGE, charge]] + ([[FIRST, first]] if first is not None else [])  # lists, as from xdist
        for when in ("setup", "call", "teardown"):  # only the call report carries a charge
            report = SimpleNamespace(nodeid=nodeid, when=when, duration=9.0, user_properties=props)
            budget.pytest_runtest_logreport(report)  # type: ignore[arg-type]
    budget.pytest_runtest_logreport(
        SimpleNamespace(nodeid="x::failed_setup", when="call", user_properties=[])
    )  # type: ignore[arg-type]
    assert budget.over() == [
        ("tests/unit/test_a.py::twice_more[x]", 0.9, 0.8),
        ("tests/unit/test_a.py::twice", 0.7, 0.6),
    ]
