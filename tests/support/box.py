"""FakeBox: an in-memory Box. A file tree under /work, every exec recorded, answers scripted or from a tiny
interpreter for the commands the tests use (`cat`, `mkdir -p`, `test`, `find`; a test registers more in
`handlers`, keyed by the command's first word)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from airlie.boxes.protocol import Box, BoxUnavailable, ExecResult, Network

Handler = Callable[["FakeBox", Sequence[str], bytes | None], ExecResult]


@dataclass
class Call:
    argv: tuple[str, ...]
    env: dict[str, str]
    stdin: bytes | None
    cwd: str | None


@dataclass
class FakeBox:
    name: str
    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    responses: dict[tuple[str, ...], ExecResult] = field(default_factory=dict)
    handlers: dict[str, Handler] = field(default_factory=dict)
    state: Literal["absent", "stopped", "running"] = "absent"
    boots: list[Network] = field(default_factory=list)
    deleted: bool = False
    fail_boot: str | None = None
    created: list[tuple[str, int, float, int]] = field(default_factory=list)

    async def exists(self) -> bool:
        return self.state != "absent"

    async def running(self) -> bool:
        return self.state == "running"

    async def create(self, image: str, *, cpus: int, memory_gb: float, disk_gb: int) -> None:
        self.created.append((image, cpus, memory_gb, disk_gb))
        self.state = "stopped"

    async def boot(self, *, network: Network, timeout_s: float) -> None:
        if self.fail_boot is not None:
            raise BoxUnavailable(self.fail_boot)
        self.boots.append(network)
        self.state = "running"

    async def exec(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] = {},  # noqa: B006
        cwd: str | None = None,
        stdin: bytes | None = None,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        self.calls.append(Call(tuple(argv), dict(env), stdin, cwd))
        scripted = self.responses.get(tuple(argv))
        return scripted if scripted is not None else self._interpret(argv, stdin)

    async def exec_bytes(
        self, argv: Sequence[str], *, stdin: bytes | None = None, timeout_s: float = 120.0
    ) -> tuple[int | None, bytes]:
        self.calls.append(Call(tuple(argv), {}, stdin, None))
        if list(argv[:1]) == ["cat"] and len(argv) == 2 and argv[1] in self.files:
            return 0, self.files[argv[1]]
        result = self.responses.get(tuple(argv)) or self._interpret(argv, stdin)
        return result.rc, result.output.encode()

    async def stop(self) -> None:
        if self.state == "running":
            self.state = "stopped"

    async def delete(self) -> None:
        self.state = "absent"
        self.deleted = True
        self.files.clear()

    def _interpret(self, argv: Sequence[str], stdin: bytes | None) -> ExecResult:
        if not argv:
            return ExecResult(None, "empty command")
        handler = self.handlers.get(argv[0])
        if handler is not None:
            return handler(self, argv, stdin)
        match list(argv):
            case ["cat", path] if path in self.files:
                return ExecResult(0, self.files[path].decode())
            case ["cat", path]:
                return ExecResult(1, f"cat: {path}: No such file or directory")
            case ["mkdir", "-p", *_]:
                return ExecResult(0, "")
            case ["test", "-e" | "-f", path]:
                return ExecResult(0 if path in self.files else 1, "")
            case ["test", "-d", path]:
                prefix = path.rstrip("/") + "/"
                return ExecResult(0 if any(f.startswith(prefix) for f in self.files) else 1, "")
            case ["find", root, *_]:
                prefix = root.rstrip("/") + "/"
                return ExecResult(0, "".join(f"{f}\n" for f in sorted(self.files) if f.startswith(prefix)))
        return ExecResult(127, f"{argv[0]}: not scripted")


_: Box = FakeBox("x")
