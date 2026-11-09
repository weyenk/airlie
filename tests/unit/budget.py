"""The unit-test budget (AIRLIE_TEST_BUDGET_MS): each test's call phase is charged the CPU time it cost, and a
test over the budget is run once more before it counts. tests/unit/conftest.py registers it; the integration
tests (tests/integration/test_unit_budget.py) register it in a project of their own.

CPU time, not wall clock: a loaded machine (a busy Mac, CI's 2-vCPU runners) stretched the wall-clock duration
of tests that cost no more there, and the budget flagged them. The charge is the worker process's own CPU
(`time.process_time()`, every thread of it) plus the CPU of the child processes that ended and were waited for
during the call (`RUSAGE_CHILDREN`: git, curl, uv a test ran). It does NOT see time spent blocked: a sleep,
a wait on a socket or on the network, a child still running when the call returns (or one never waited for).
The tests that lean on the real clock are quarantined separately (`clock:` in tests/unit/quarantine.toml). A
thread an earlier test left running is charged to whichever test is running beside it.

One retry: a test whose call passed but cost more than the budget is run again at once, setup, call and
teardown, with fresh function fixtures (session and module ones are kept, as for any next test). The second
run's reports are the ones logged, so a re-run that fails fails the test. It counts against the budget only if
the second run is over too, and the report names both. A test whose first run failed, errored or was skipped
is not run again, and nothing is retried when AIRLIE_TEST_BUDGET_MS is unset (this plugin is not registered).

Under xdist each worker measures and retries its own tests and puts the charge on the call report's
`user_properties`, which xdist sends to the controller; the controller (or the one process of a serial run)
sums them up and fails the session."""

from __future__ import annotations

import os
import resource
import time
from collections.abc import Callable, Generator
from typing import Any

import pytest
from _pytest.runner import runtestprotocol

ENV = "AIRLIE_TEST_BUDGET_MS"
CPU = pytest.StashKey[float]()
CHARGE, FIRST = "airlie_cpu_seconds", "airlie_cpu_seconds_first"
"""The call report's user_properties: the charge that counts, and the first run's when the test was re-run."""


def cpu_seconds() -> float:
    """This process's CPU so far plus that of its children that ended and were waited for."""
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time() + children.ru_utime + children.ru_stime


class CpuClock:
    """Runs in the process that runs the tests (each xdist worker, or a serial run): charges each call its CPU
    seconds and re-runs a test over the budget once."""

    def __init__(self, limit: float, exempt: Callable[[str], bool]) -> None:
        self.limit, self.exempt = limit, exempt

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        start = cpu_seconds()
        try:
            return (yield)
        finally:
            item.stash[CPU] = cpu_seconds() - start

    def run(self, item: pytest.Item, nextitem: pytest.Item | None) -> tuple[list[pytest.TestReport], float]:
        item.stash[CPU] = -1.0  # the call never ran (setup failed or skipped)
        reports = runtestprotocol(item, nextitem=nextitem, log=False)
        return reports, item.stash[CPU]

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> bool:
        """The default protocol (logstart, setup/call/teardown, logfinish), with the reports held back until
        it is known whether the test runs again."""
        item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        reports, charge = self.run(item, nextitem)
        first = None
        if charge > self.limit and all(r.passed for r in reports) and not self.exempt(item.nodeid):
            first = charge
            reports, charge = self.run(item, nextitem)
        props = [(CHARGE, charge)] if charge >= 0 else []
        props += [(FIRST, first)] if first is not None else []
        for report in reports:  # every phase's report carries it: junitxml reads the teardown's
            report.user_properties = [*report.user_properties, *props]  # a copy: theirs is the item's list
            item.ihook.pytest_runtest_logreport(report=report)
        item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
        return True


class Budget:
    """Runs where the reports end up (the xdist controller, or a serial run): the CPU charge of each test's
    call phase, from its call report. Setup and teardown are not charged, so a worker's cold imports and
    session fixtures are not billed to its first test. A plugin object, not module state: mutmut runs
    pytest.main many times in one process, and the conftest stays imported between them."""

    def __init__(self, limit: float, exempt: Callable[[str], bool]) -> None:
        self.limit, self.exempt = limit, exempt
        self.charges: dict[str, tuple[float | None, float]] = {}
        """{node id: (first run's seconds if it was re-run, the seconds that count)}"""

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when != "call":
            return
        props = {str(k): v for k, v in report.user_properties}  # xdist turns the pairs into lists
        if CHARGE in props:
            self.charges[report.nodeid] = (props.get(FIRST), float(props[CHARGE]))

    def over(self) -> list[tuple[str, float | None, float]]:
        """The tests whose counted charge is over the budget and that are not exempt, costliest first."""
        slow = [
            (n, first, s) for n, (first, s) in self.charges.items() if s > self.limit and not self.exempt(n)
        ]
        return sorted(slow, key=lambda row: -row[2])

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.over() and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        slow = self.over()
        if not slow:
            return
        ms = round(self.limit * 1000)
        terminalreporter.section(f"over the {ms} ms CPU budget twice ({ENV})", red=True)
        for nodeid, first, seconds in slow:
            took = (
                f"{seconds * 1000:.0f} ms"
                if first is None
                else f"{first * 1000:.0f} ms, {seconds * 1000:.0f} ms"
            )
            terminalreporter.write_line(f"{took:>18}  {nodeid}")
        terminalreporter.write_line(
            "make each one cheaper, or quarantine it in tests/unit/quarantine.toml with a `slow: <ms>` reason"
        )


BUDGET = pytest.StashKey[Budget]()


def register(config: pytest.Config, exempt: Callable[[str], bool]) -> None:
    """Turn the budget on when AIRLIE_TEST_BUDGET_MS is set: the clock where tests run, the tally where the
    reports arrive (both in a serial run)."""
    raw = os.environ.get(ENV)
    if not raw:
        return
    limit = float(raw) / 1000
    config.pluginmanager.register(CpuClock(limit, exempt), "airlie-test-cpu-clock")
    if not hasattr(config, "workerinput"):
        config.stash[BUDGET] = budget = Budget(limit, exempt)
        config.pluginmanager.register(budget, "airlie-test-budget")
