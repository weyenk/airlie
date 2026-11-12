"""A scripted host-process runner and spawner, so TartBox tests start no `tart`."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

Reply = tuple[int | None, bytes, bytes]
"""(rc, stdout, stderr), as a Runner answers."""


@dataclass
class FakeRunner:
    """Records (argv, stdin, timeout); answers by the first scripted prefix that matches, else rc 0."""

    replies: list[tuple[tuple[str, ...], Reply | Callable[[], Reply]]] = field(default_factory=list)
    calls: list[tuple[tuple[str, ...], bytes | None, float]] = field(default_factory=list)
    limits: list[int] = field(default_factory=list)
    """The output cap each call asked for."""
    missing: bool = False

    def reply(self, prefix: Sequence[str], rc: int | None, out: bytes = b"", err: bytes = b"") -> None:
        self.replies.append((tuple(prefix), (rc, out, err)))

    async def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout_s: float, limit: int = 4 * 1024 * 1024
    ) -> Reply:
        if self.missing:
            raise FileNotFoundError(argv[0])
        self.calls.append((tuple(argv), stdin, timeout_s))
        self.limits.append(limit)
        for prefix, answer in self.replies:
            if tuple(argv[: len(prefix)]) == prefix:
                return answer() if callable(answer) else answer
        return 0, b"", b""

    def argvs(self) -> list[list[str]]:
        return [list(c[0]) for c in self.calls]


def info(*, running: bool) -> bytes:
    return json.dumps({"Running": running, "State": "running" if running else "stopped"}).encode()


@dataclass
class FakeProc:
    returncode: int | None = None
    killed: bool = False
    waited: bool = False

    async def wait(self) -> int:
        self.waited = True
        self.returncode = 0
        return 0

    async def kill(self) -> None:
        self.killed = True


@dataclass
class FakeSpawner:
    proc: FakeProc = field(default_factory=FakeProc)
    spawned: list[tuple[list[str], Path]] = field(default_factory=list)
    log_text: str | None = None

    async def __call__(self, argv: Sequence[str], log: Path) -> FakeProc:
        self.spawned.append((list(argv), log))
        if self.log_text is not None:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(self.log_text)
        return self.proc


class Clock:
    """A fake monotonic clock that only `sleep` (or `advance`) moves."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds
