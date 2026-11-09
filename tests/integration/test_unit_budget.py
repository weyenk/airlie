"""The unit-test CPU budget (tests/unit/budget.py) in real pytest runs: a project of its own with a 100 ms
budget, run serially and under xdist. Here, not in tests/unit: each test starts pytest (and its workers) in
child processes, whose CPU the unit budget would rightly charge to it."""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CONFTEST = """
from tests.unit import budget


def pytest_configure(config):
    budget.register(config, lambda nodeid: nodeid.endswith("_exempt"))
"""

TESTS = '''
import os
import subprocess
import sys
import time
from pathlib import Path

BURN = """
import time
end = time.process_time() + 0.15
while time.process_time() < end:
    pass
"""


def burn():
    exec(BURN)


def runs(name):
    """How many times this test has started, this one included."""
    mark = Path(os.environ["MARKS"]) / name
    with mark.open("a") as f:
        f.write("x")
    return len(mark.read_text())


def test_cheap():
    runs("cheap")


def test_busy_twice():
    runs("busy_twice")
    burn()


def test_busy_once():
    if runs("busy_once") == 1:
        burn()


def test_sleeps():
    runs("sleeps")
    time.sleep(0.3)


def test_child_burns():
    runs("child_burns")
    subprocess.run([sys.executable, "-c", BURN], check=True)


def test_fails_on_its_rerun():
    if runs("fails_on_its_rerun") == 1:
        burn()
    else:
        raise AssertionError("the re-run failed")


def test_busy_exempt():
    runs("busy_exempt")
    burn()
'''


def run(
    tmp_path: Path, *args: str, budget: str | None = "100"
) -> tuple[subprocess.CompletedProcess[str], Path]:
    project, marks = tmp_path / "project", tmp_path / "marks"
    project.mkdir()
    marks.mkdir()
    (project / "conftest.py").write_text(CONFTEST)
    (project / "test_work.py").write_text(TESTS)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "AIRLIE_TEST_"))}
    env |= {"PYTHONPATH": str(ROOT), "MARKS": str(marks), "NO_COLOR": "1"}
    env.pop("FORCE_COLOR", None)
    if budget is not None:
        env["AIRLIE_TEST_BUDGET_MS"] = budget
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "--junitxml",
        str(tmp_path / "junit.xml"),
    ]
    done = subprocess.run([*cmd, *args], cwd=project, env=env, capture_output=True, text=True, timeout=120)
    return done, marks


def outcomes(tmp_path: Path) -> dict[str, tuple[str, dict[str, float]]]:
    """{test name: (passed or failed, its user_properties)} from the run's JUnit XML."""
    found = {}
    for case in ET.parse(tmp_path / "junit.xml").iter("testcase"):
        props = {p.get("name", ""): float(p.get("value", "")) for p in case.iter("property")}
        found[case.get("name", "")] = ("failed" if case.find("failure") is not None else "passed", props)
    return found


def starts(marks: Path) -> dict[str, int]:
    return {mark.name: len(mark.read_text()) for mark in marks.iterdir()}


@pytest.mark.parametrize("args", [[], ["-n", "auto", "--maxprocesses", "2"]], ids=["serial", "xdist"])
def test_only_a_test_over_the_cpu_budget_twice_fails_the_session_and_both_charges_are_named(
    tmp_path: Path, args: list[str]
) -> None:
    done, marks = run(tmp_path, *args)
    out = done.stdout
    assert done.returncode == 1, out
    found = outcomes(tmp_path)
    # Each test over the budget ran twice, the rest once; the exempt one is never re-run.
    assert starts(marks) == {"cheap": 1, "busy_twice": 2, "busy_once": 2, "sleeps": 1, "child_burns": 2,
                             "fails_on_its_rerun": 2, "busy_exempt": 1}  # fmt: skip
    assert found["test_busy_twice"][0] == "passed"
    first, second = (
        found["test_busy_twice"][1]["airlie_cpu_seconds_first"],
        found["test_busy_twice"][1]["airlie_cpu_seconds"],
    )
    assert first > 0.1 and second > 0.1
    assert found["test_busy_once"][1]["airlie_cpu_seconds"] < 0.1  # over once, then under: it passes
    assert found["test_sleeps"][1]["airlie_cpu_seconds"] < 0.1  # a 0.3 s sleep costs no CPU
    assert found["test_child_burns"][1]["airlie_cpu_seconds"] > 0.1  # the child's CPU is charged
    assert found["test_fails_on_its_rerun"][0] == "failed"  # the re-run's failure is not swallowed
    section = out[out.index("over the 100 ms CPU budget twice") :]
    named = [line.split()[-1] for line in section.splitlines()[1:] if " ms  " in line]
    assert sorted(named) == ["test_work.py::test_busy_twice", "test_work.py::test_child_burns"]
    assert f"{first * 1000:.0f} ms, {second * 1000:.0f} ms" in section


def test_with_no_budget_set_nothing_is_charged_or_run_twice(tmp_path: Path) -> None:
    done, marks = run(tmp_path, budget=None)
    assert done.returncode == 0, done.stdout
    assert set(starts(marks).values()) == {1}
    assert all(props == {} for _, props in outcomes(tmp_path).values())
    assert "CPU budget" not in done.stdout


@pytest.mark.parametrize(("chosen", "code"), [("busy_twice", 1), ("busy_once or sleeps", 0)])
def test_the_budget_alone_decides_the_session(tmp_path: Path, chosen: str, code: int) -> None:
    done, _ = run(tmp_path, "-k", chosen)
    assert done.returncode == code, done.stdout
    assert ("passed" in done.stdout) and ("failed" not in done.stdout)  # every test itself passed
