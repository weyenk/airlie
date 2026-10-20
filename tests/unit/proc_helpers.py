"""Processes for tests: a sleeper no other process shares the command line of, and whether one is running."""

import asyncio
import random
import subprocess
import time
from collections.abc import Callable


def running(pattern: str) -> bool:
    """Whether some process has `pattern` in its command line."""
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


async def eventually(check: Callable[[], bool], within: float = 5.0) -> bool:
    """`check` turned true within `within` seconds (a killed process is reaped a moment later)."""
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def unique_sleep() -> str:
    """A `sleep 30` that no other process on the machine has the command line of."""
    return f"sleep 30.{random.randrange(10**8, 10**9)}"


class StartGate:
    """Puts a test in charge of where a cancel lands in a command's start, instead of the machine's speed.

    It wraps `airlie.subproc.started` where `modules` call it. `spawned` is set once the child exists (its
    pipes not yet connected); with `hold`, the start then waits for `release` before it returns, so a cancel
    the test sends after `spawned` lands mid-start, whatever the machine. `ran` is set once the start has
    returned: a cancel sent after it lands while the command runs (PR 105's CI: a slow runner's cancels all
    landed mid-start, and the other path went untested)."""

    def __init__(self, monkeypatch: object, *modules: object, hold: bool) -> None:
        from airlie import subproc

        self.spawned = asyncio.Event()
        self.release = asyncio.Event()
        self.ran = asyncio.Event()
        real = subproc.started

        async def held(starting):  # type: ignore[no-untyped-def]
            proc = await starting
            self.spawned.set()
            if hold:
                await self.release.wait()
            return proc

        async def gated(starting):  # type: ignore[no-untyped-def]
            proc = await real(held(starting))
            self.ran.set()
            return proc

        for module in modules:
            monkeypatch.setattr(module, "started", gated)  # type: ignore[attr-defined]


async def cancel_mid_start(task: asyncio.Task[object], gate: StartGate, sleeper: str) -> None:
    """Cancel `task` while its command is being started (the child, and its `sleeper`, already running),
    then let the start return: the cancel is already on it, so the start's own cleanup must kill it."""
    await asyncio.wait_for(gate.spawned.wait(), 20)
    assert await eventually(lambda: running(f"^{sleeper}"))  # the sleep itself is up, not only its shell
    task.cancel()
    gate.release.set()


async def cancel_once_running(task: asyncio.Task[object], gate: StartGate, sleeper: str) -> None:
    """Cancel `task` once its command's start has returned: the cancel lands in the wait for its output."""
    await asyncio.wait_for(gate.ran.wait(), 20)
    assert await eventually(lambda: running(f"^{sleeper}"))
    task.cancel()


def cancel_phases() -> list[object]:
    """The two places a cancel can land in a command's start, as pytest parameters (hold, cancel)."""
    import pytest

    return [
        pytest.param(True, cancel_mid_start, id="mid-start"),
        pytest.param(False, cancel_once_running, id="once-running"),
    ]
