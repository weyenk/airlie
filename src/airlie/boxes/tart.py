"""TartBox: the Box seam on Tart Linux VMs. Every `tart` process starts here, through a Runner (a command that
ends) or a Spawner (the long-running `tart run`), so tests never start one."""

from __future__ import annotations

import asyncio
import json
import shlex
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from airlie.boxes.protocol import BoxUnavailable, ExecResult, Network
from airlie.subproc import kill_group, started

Runner = Callable[[Sequence[str], bytes | None, float], Awaitable[tuple[int | None, bytes]]]
"""Runs a host process (argv, stdin, timeout) and returns (rc, stdout+stderr); rc None when it timed out."""

INSTALL_HINT = (
    "Tart is not installed: brew install cirruslabs/cli/tart "
    "(airlie repository chats run in a Tart VM and never on the host)"
)
LOG_TAIL_CHARS = 2000
STOP_WAIT_S = 60.0


class RealRunner:
    async def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout_s: float
    ) -> tuple[int | None, bytes]:
        proc = await started(
            asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # its own process group: kill_group reaches what it starts
            )
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout_s)
        except TimeoutError:
            await kill_group(proc)
            return None, b""
        except BaseException:  # cancelled: nothing it started may outlive its caller
            await kill_group(proc)
            raise
        return proc.returncode, out


real_runner = RealRunner()


class Spawned(Protocol):
    """A detached host process kept on the box."""

    @property
    def returncode(self) -> int | None: ...
    async def wait(self) -> int: ...
    async def kill(self) -> None: ...


Spawner = Callable[[Sequence[str], Path], Awaitable[Spawned]]
"""Starts argv detached with its output appended to the log file."""


class _Proc:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def wait(self) -> int:
        return await self._proc.wait()

    async def kill(self) -> None:
        await kill_group(self._proc)


async def real_spawn(argv: Sequence[str], log: Path) -> Spawned:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        proc = await started(
            asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                start_new_session=True,
            )
        )
    return _Proc(proc)


async def dns_resolve(host: str) -> list[str]:
    """The IPv4 addresses of `host` as /32 CIDRs."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET)
    return sorted({f"{info[4][0]}/32" for info in infos})


class TartBox:
    def __init__(
        self,
        name: str,
        *,
        run: Runner = real_runner,
        tart: str = "tart",
        spawn: Spawner = real_spawn,
        resolve: Callable[[str], Awaitable[list[str]]] = dns_resolve,
        log_dir: Path,
        poll_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self._run = run
        self._tart = tart
        self._spawn = spawn
        self._resolve = resolve
        self._log = log_dir / f"{name}.log"
        self._poll_s = poll_s
        self._sleep = sleep
        self._proc: Spawned | None = None

    async def _tart_run(
        self, *args: str, stdin: bytes | None = None, timeout_s: float = 120.0
    ) -> tuple[int | None, bytes]:
        try:
            return await self._run([self._tart, *args], stdin, timeout_s)
        except FileNotFoundError as e:
            raise BoxUnavailable(INSTALL_HINT) from e

    async def _info(self) -> dict[str, object] | None:
        rc, out = await self._tart_run("get", self.name, "--format", "json", timeout_s=30.0)
        if rc != 0:
            return None
        try:
            info = json.loads(out)
        except json.JSONDecodeError:
            return None
        return info if isinstance(info, dict) else None

    async def exists(self) -> bool:
        return await self._info() is not None

    async def running(self) -> bool:
        info = await self._info()
        return bool(info and info.get("Running"))

    async def create(self, image: str, *, cpus: int, memory_gb: float, disk_gb: int) -> None:
        rc, out = await self._tart_run("clone", image, self.name, timeout_s=3600.0)
        if rc != 0:
            raise BoxUnavailable(f"tart clone {image} failed: {out.decode('utf-8', 'replace').strip()}")
        rc, out = await self._tart_run(
            "set",
            self.name,
            "--cpu",
            str(cpus),
            "--memory",
            str(int(memory_gb * 1024)),
            "--disk-size",
            str(disk_gb),
        )
        if rc != 0:
            raise BoxUnavailable(f"tart set {self.name} failed: {out.decode('utf-8', 'replace').strip()}")

    async def _network_flags(self, network: Network) -> list[str]:
        if network.mode == "open":
            return []
        if network.mode == "off":
            return ["--net-host"]
        cidrs: list[str] = []
        for entry in network.allow:  # a CIDR as it is, a host name resolved
            for cidr in [entry] if "/" in entry else await self._resolve(entry):
                if cidr not in cidrs:
                    cidrs.append(cidr)
        flags = ["--net-softnet-block=0.0.0.0/0"]
        if cidrs:
            flags.append(f"--net-softnet-allow={','.join(cidrs)}")
        return flags

    def _log_tail(self) -> str:
        try:
            return self._log.read_text(errors="replace")[-LOG_TAIL_CHARS:].strip()
        except OSError:
            return "(no log)"

    async def boot(self, *, network: Network, timeout_s: float) -> None:
        argv = [self._tart, "run", "--no-graphics", *await self._network_flags(network), self.name]
        try:
            self._proc = await self._spawn(argv, self._log)
        except FileNotFoundError as e:
            raise BoxUnavailable(INSTALL_HINT) from e
        for _ in range(max(1, int(timeout_s / self._poll_s))):
            rc, _out = await self._tart_run("exec", self.name, "true", timeout_s=self._poll_s + 10)
            if rc == 0:
                return
            if self._proc.returncode is not None:  # tart run ended: nothing will ever answer
                break
            await self._sleep(self._poll_s)
        await self._proc.kill()
        raise BoxUnavailable(
            f"{self.name} did not answer within {timeout_s:g}s; "
            f"tart run log ({self._log}):\n{self._log_tail()}"
        )

    def _exec_args(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: str | None, *, stdin: bool, quiet: bool
    ) -> list[str]:
        script = f"cd {shlex.quote(cwd)} && " if cwd else ""
        script += 'exec "$@"' + (" 2>/dev/null" if quiet else "")
        # `env -i`: the guest command gets these pairs and nothing else, whatever the host's environment is
        return [
            "exec",
            *(["-i"] if stdin else []),
            self.name,
            "env",
            "-i",
            *(f"{k}={v}" for k, v in env.items()),
            "sh",
            "-c",
            script,
            "sh",
            *argv,
        ]

    async def exec(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] = {},  # noqa: B006 - never mutated
        cwd: str | None = None,
        stdin: bytes | None = None,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        args = self._exec_args(argv, env, cwd, stdin=stdin is not None, quiet=False)
        rc, out = await self._tart_run(*args, stdin=stdin, timeout_s=timeout_s)
        if rc is None:
            return ExecResult(None, f"error: command timed out after {timeout_s:g}s")
        return ExecResult(rc, out.decode("utf-8", "replace"))

    async def exec_bytes(
        self, argv: Sequence[str], *, stdin: bytes | None = None, timeout_s: float = 120.0
    ) -> tuple[int | None, bytes]:
        """stdout only: stderr goes to /dev/null in the guest, so it cannot corrupt a bundle."""
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
        args = self._exec_args(argv, env, None, stdin=stdin is not None, quiet=True)
        return await self._tart_run(*args, stdin=stdin, timeout_s=timeout_s)

    async def stop(self) -> None:
        await self._tart_run("stop", self.name, timeout_s=STOP_WAIT_S + 30)
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), STOP_WAIT_S)
            except TimeoutError:
                await proc.kill()

    async def delete(self) -> None:
        if await self.running():
            await self.stop()
        await self._tart_run("delete", self.name)
