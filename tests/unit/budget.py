"""The unit-test budget (AIRLIE_TEST_BUDGET_MS): each test's call phase is charged the CPU time it cost, and a
test over the budget is measured once more, alone, before it counts. tests/unit/conftest.py registers it; the
integration tests (tests/integration/test_unit_budget.py) register it in a project of their own.

CPU time, not wall clock: a loaded machine (a busy Mac, CI's 2-vCPU runners) stretched the wall-clock duration
of tests that cost no more there, and the budget flagged them. The charge is the worker process's own CPU
(`time.process_time()`, every thread of it) plus the CPU of the child processes that ended and were waited for
during the call (`RUSAGE_CHILDREN`: git, curl, uv a test ran). It does NOT see time spent blocked: a sleep,
a wait on a socket or on the network, a child still running when the call returns (or one never waited for).
The tests that lean on the real clock are quarantined separately (`clock:` in tests/unit/quarantine.toml). A
thread an earlier test left running is charged to whichever test is running beside it.

CPU time still grows under load (on this Mac a contended worker lands on an efficiency core, which spends more
CPU seconds on the same work), so a test over the budget is re-measured once at the end of the session, after
every worker has finished: one at a time, each in a fresh `python -m pytest <node id> -p no:xdist` of its own
with this budget on. It counts only if that measure is over the budget too (or fails, or is never taken: the
re-measures stop after REMEASURE_S, and the rest count as over). A fresh process would pay the imports a
warm worker had already paid, so there the test runs once unmeasured first (WarmUp). A test whose call failed
is not re-measured, and nothing runs when AIRLIE_TEST_BUDGET_MS is unset (this plugin is not registered).

Under xdist each worker measures its own tests and puts the charge on the call report's `user_properties`,
which xdist sends to the controller; the controller (or the one process of a serial run) re-measures and fails
the session."""

from __future__ import annotations

import os
import resource
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from _pytest.runner import runtestprotocol

ENV = "AIRLIE_TEST_BUDGET_MS"
CPU = pytest.StashKey[float]()
CHARGE = "airlie_cpu_seconds"
"""The call report's user_properties: the call's CPU seconds."""
REMEASURING = "AIRLIE_TEST_BUDGET_REMEASURING"
"""Set in a re-measure's own pytest, which only measures."""
REMEASURE_S = 60.0
"""The most wall-clock time one session's re-measures take; the offenders left count as over."""


def cpu_seconds() -> float:
    """This process's CPU so far plus that of its children that ended and were waited for."""
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time() + children.ru_utime + children.ru_stime


class CpuClock:
    """Runs in the process that runs the tests (each xdist worker, or a serial run): charges each call its CPU
    seconds, on the call report's user_properties."""

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        start = cpu_seconds()
        try:
            return (yield)
        finally:
            item.stash[CPU] = cpu_seconds() - start

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        report = yield
        if call.when in ("call", "teardown") and CPU in item.stash:  # junitxml reads the teardown's
            report.user_properties = [
                *report.user_properties,
                (CHARGE, item.stash[CPU]),
            ]  # not the item's list
        return report


class WarmUp:
    """In a re-measure's own pytest: the test runs once to warm the process up (its lazy imports, its caches,
    as a worker that had run other tests was), then again with fresh function fixtures, and only the second
    run's reports are logged, so it is the second that is measured and a second run that fails is a failure.
    A test whose first run did not pass is logged as it ran."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> bool:
        item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        reports = runtestprotocol(item, nextitem=nextitem, log=False)
        if all(r.passed for r in reports):
            if CPU in item.stash:
                del item.stash[CPU]
            reports = runtestprotocol(item, nextitem=nextitem, log=False)
        for report in reports:
            item.ihook.pytest_runtest_logreport(report=report)
        item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
        return True


class Budget:
    """Runs where the reports end up (the xdist controller, or a serial run): the CPU charge of each test's
    call phase, from its call report, and the re-measure of each that passed over the budget. Setup and
    teardown are not charged, so a worker's cold imports and session fixtures are not billed to its first
    test. A plugin object, not module state: mutmut runs pytest.main many times in one process, and the
    conftest stays imported between them."""

    def __init__(self, limit: float, exempt: Callable[[str], bool], remeasure: bool = True) -> None:
        self.limit, self.exempt, self.remeasure = limit, exempt, remeasure
        self.charges: dict[str, float] = {}
        """{node id: the first run's seconds}, of the tests whose call passed"""
        self.again: dict[str, float | str] = {}
        """{node id: the re-measured seconds, or why there are none}"""

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when != "call" or not report.passed:
            return
        props = {str(k): v for k, v in report.user_properties}  # xdist turns the pairs into lists
        if CHARGE in props:
            self.charges[report.nodeid] = float(props[CHARGE])

    def offenders(self) -> list[str]:
        """The tests over the budget on their first run that are not exempt, costliest first."""
        slow = [n for n, s in self.charges.items() if s > self.limit and not self.exempt(n)]
        return sorted(slow, key=lambda n: -self.charges[n])

    def remeasure_all(self, config: pytest.Config) -> None:
        """Measure each offender once more, one at a time, each in a pytest of its own, until REMEASURE_S
        have gone: the ones left are named not re-measured and count as over."""
        deadline = time.monotonic() + REMEASURE_S
        for nodeid in self.offenders():
            left = deadline - time.monotonic()
            self.again[nodeid] = remeasure(config, nodeid, left) if left > 0 else "not re-measured (time)"

    def over(self) -> list[tuple[str, float, float | str | None]]:
        """(node id, first run's seconds, re-measured seconds or why there are none) of each test over the
        budget both times, costliest first. In a re-measure itself, the one measure alone."""
        if not self.remeasure:
            return [(n, self.charges[n], None) for n in self.offenders()]
        found: list[tuple[str, float, float | str | None]] = []
        for nodeid in self.offenders():
            again = self.again.get(nodeid, "not re-measured")
            if isinstance(again, str) or again > self.limit:
                found.append((nodeid, self.charges[nodeid], again))
        return found

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.remeasure and self.offenders():
            self.remeasure_all(session.config)
        if self.over() and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.remeasure and self.again:
            calm = sum(1 for s in self.again.values() if not isinstance(s, str) and s <= self.limit)
            terminalreporter.write_line(
                f"{len(self.again)} over the CPU budget re-measured alone; {calm} came in under it"
            )
        slow = self.over()
        if not slow:
            return
        ms = round(self.limit * 1000)
        terminalreporter.section(f"over the {ms} ms CPU budget twice ({ENV})", red=True)
        for nodeid, first, again in slow:
            second = again if isinstance(again, str) or again is None else f"{again * 1000:.0f} ms alone"
            took = f"{first * 1000:.0f} ms" + ("" if second is None else f", {second}")
            terminalreporter.write_line(f"{took:>24}  {nodeid}")
        terminalreporter.write_line(
            "make each one cheaper, or quarantine it in tests/unit/quarantine.toml with a `slow: <ms>` reason"
        )


def remeasure(config: pytest.Config, nodeid: str, timeout: float) -> float | str:
    """One test's call-phase CPU in a fresh `python -m pytest <node id> -p no:xdist` of its own, with this
    budget on (and its re-measure off): the seconds, or why there are none."""
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        cmd = [sys.executable, "-m", "pytest", nodeid, "-p", "no:xdist", "-p", "no:cacheprovider", "-q"]
        try:
            subprocess.run(
                [*cmd, "--junitxml", str(junit)],
                cwd=config.rootpath,
                env={**os.environ, REMEASURING: "1"},
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "not re-measured (time)"
        if not junit.is_file():
            return "not re-measured (no run)"
        for case in ET.parse(junit).iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                return "failed when re-measured"
            for prop in case.iter("property"):
                if prop.get("name") == CHARGE:
                    return float(prop.get("value", "0"))
        return "not re-measured (no run)"


BUDGET = pytest.StashKey[Budget]()


def register(config: pytest.Config, exempt: Callable[[str], bool]) -> None:
    """Turn the budget on when AIRLIE_TEST_BUDGET_MS is set: the clock where tests run, the tally where the
    reports arrive (both in a serial run)."""
    raw = os.environ.get(ENV)
    if not raw:
        return
    limit = float(raw) / 1000
    config.pluginmanager.register(CpuClock(), "airlie-test-cpu-clock")
    if os.environ.get(REMEASURING):
        config.pluginmanager.register(WarmUp(), "airlie-test-budget-warm-up")
    if not hasattr(config, "workerinput"):
        config.stash[BUDGET] = budget = Budget(limit, exempt, remeasure=not os.environ.get(REMEASURING))
        config.pluginmanager.register(budget, "airlie-test-budget")
