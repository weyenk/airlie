"""FakeBox: an in-memory Box. A file tree under /work, every exec recorded, answers scripted or from a tiny
interpreter for the commands the tests use (`cat`, `tee`, `sh -c 'cat > path'`, `mkdir -p`, `test`, `find`; a
test registers more in `handlers`, keyed by the command's first word). It follows the real lifecycle: exec
needs a running box, boot needs a created one, create needs an absent one."""

from __future__ import annotations

import re
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
    dirs: set[str] = field(default_factory=set)
    timeouts: set[tuple[str, ...]] = field(default_factory=set)
    """Commands (whole argv) that time out: rc None, as the real box answers."""
    timed_out: list[tuple[tuple[str, ...], float]] = field(default_factory=list)
    stderr: dict[tuple[str, ...], bytes] = field(default_factory=dict)
    """What `exec_bytes` returns in place of stdout when the command fails."""

    async def exists(self) -> bool:
        return self.state != "absent"

    async def running(self) -> bool:
        return self.state == "running"

    async def create(self, image: str, *, cpus: int, memory_gb: float, disk_gb: int) -> None:
        if self.state != "absent":
            raise BoxUnavailable(f"box {self.name} already exists")
        self.created.append((image, cpus, memory_gb, disk_gb))
        self.state = "stopped"

    async def boot(self, *, network: Network, timeout_s: float) -> None:
        if self.state == "absent":
            raise BoxUnavailable(f"box {self.name} does not exist")
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
        if self.state != "running":
            return ExecResult(None, f"error: box {self.name} is not running")
        if tuple(argv) in self.timeouts:
            self.timed_out.append((tuple(argv), timeout_s))
            return ExecResult(None, f"error: command timed out after {timeout_s:g}s")
        scripted = self.responses.get(tuple(argv))
        return scripted if scripted is not None else self._interpret(argv, stdin)

    async def exec_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        stdin: bytes | None = None,
        timeout_s: float = 120.0,
    ) -> tuple[int | None, bytes]:
        self.calls.append(Call(tuple(argv), {}, stdin, cwd))
        if self.state != "running":
            return None, f"error: box {self.name} is not running".encode()
        if tuple(argv) in self.timeouts:
            self.timed_out.append((tuple(argv), timeout_s))
            return None, self.stderr.get(tuple(argv), b"")
        if list(argv[:1]) == ["cat"] and len(argv) == 2 and argv[1] in self.files:
            return 0, self.files[argv[1]]
        result = self.responses.get(tuple(argv)) or self._interpret(argv, stdin)
        if result.rc != 0:
            return result.rc, self.stderr.get(tuple(argv), result.output.encode())
        return result.rc, result.output.encode()

    async def stop(self) -> None:
        if self.state == "running":
            self.state = "stopped"

    async def delete(self) -> None:
        self.state = "absent"
        self.deleted = True
        self.files.clear()
        self.dirs.clear()

    def _mkdir(self, path: str) -> None:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            self.dirs.add("/" + "/".join(parts[:i]))

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
            case ["mkdir", "-p", *paths]:
                for path in paths:
                    self._mkdir(path)
                return ExecResult(0, "")
            case ["tee", path]:
                self.files[path] = stdin or b""
                return ExecResult(0, (stdin or b"").decode())
            case ["sh", "-c", script] if m := re.fullmatch(r"cat > (\S+)", script):
                self.files[m.group(1)] = stdin or b""
                return ExecResult(0, "")
            case ["test", "-e" | "-f", path]:
                return ExecResult(0 if path in self.files else 1, "")
            case ["test", "-d", path]:
                prefix = path.rstrip("/") + "/"
                known = path.rstrip("/") in self.dirs or any(f.startswith(prefix) for f in self.files)
                return ExecResult(0 if known else 1, "")
            case ["find", root, *_]:
                prefix = root.rstrip("/") + "/"
                return ExecResult(0, "".join(f"{f}\n" for f in sorted(self.files) if f.startswith(prefix)))
        return ExecResult(127, f"{argv[0]}: not scripted")


_: Box = FakeBox("x")
