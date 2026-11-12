"""The Box seam: one VM per chat session. A backend (Tart) implements it; tests use FakeBox."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, NamedTuple, Protocol

from airlie.engine.types import EngineError


class ExecResult(NamedTuple):
    rc: int | None
    """The exit code; None when the command could not run or timed out."""
    output: str
    """stdout and stderr merged."""


class Network(NamedTuple):
    mode: Literal["open", "off", "allow_list"]
    """off: nothing is reachable (softnet blocks every address); allow_list: only `allow`."""
    allow: tuple[str, ...] = ()
    """CIDRs or host names reachable in allow_list mode."""


class BoxUnavailable(EngineError):
    """The backend cannot be used; the message says why and how to fix it."""


class ConfirmationRequired(EngineError):
    """A step that gives setup secrets to a recipe nobody has built before needs a person's yes."""

    def __init__(self, what: str) -> None:
        super().__init__(f"confirmation required: {what}")
        self.what = what


class Box(Protocol):
    name: str

    async def exists(self) -> bool:
        """The disk exists."""
        ...

    async def running(self) -> bool: ...

    async def create(self, image: str, *, cpus: int, memory_gb: float, disk_gb: int) -> None:
        """Clone `image` to `name` and size it."""
        ...

    async def boot(self, *, network: Network, timeout_s: float) -> None:
        """Start it with `network` and wait until `exec` answers."""
        ...

    async def exec(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] = {},  # noqa: B006 - never mutated
        cwd: str | None = None,
        stdin: bytes | None = None,
        timeout_s: float = 120.0,
    ) -> ExecResult: ...

    async def exec_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        stdin: bytes | None = None,
        timeout_s: float = 120.0,
    ) -> tuple[int | None, bytes]:
        """For bundles: stdout only on success; when the command fails (rc not 0, or None) the bytes are its
        stderr instead, so a failed `git bundle` has a diagnostic."""
        ...

    async def stop(self) -> None:
        """The disk stays."""
        ...

    async def rename(self, new_name: str) -> None:
        """Give the (stopped) disk another name; `name` is the new one afterwards."""
        ...

    async def delete(self) -> None: ...
