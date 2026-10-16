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
