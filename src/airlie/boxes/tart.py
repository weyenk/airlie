"""TartBox: the Box seam on Tart Linux VMs. Every `tart` process starts here, through a Runner (a command that
ends) or a Spawner (the long-running `tart run`), so tests never start one."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from airlie.boxes.protocol import OUTPUT_MAX, BackendMissing, BoxUnavailable, ExecResult, Network
from airlie.subproc import DRAIN_S, kill_group, started

Runner = Callable[[Sequence[str], bytes | None, float, int], Awaitable[tuple[int | None, bytes, bytes]]]
"""Runs a host process (argv, stdin, timeout, limit): (rc, stdout, stderr), the streams apart (`tart
exec` keeps the guest's apart too), each cut at `limit` bytes. On a timeout rc is None and the two are what it
had written by then."""

INSTALL_HINT = (
    "Tart is not installed: brew install cirruslabs/cli/tart "
    "(airlie repository chats run in a Tart VM and never on the host)"
)
SOFTNET_HINT = (
    "softnet is not installed, and boxes.network.after_setup (allow_list or off) needs it to filter the VM's "
    'network: brew install openai/tools/softnet && sudo chown root:wheel "$(brew --prefix)/bin/softnet" '
    '&& sudo chmod u+s "$(brew --prefix)/bin/softnet" (softnet needs a SUID bit or passwordless sudo, and '
    "macOS 26 or later)"
)
LOG_TAIL_CHARS = 2000
STOP_WAIT_S = 60.0
SYNC_S = 20.0
"""How long `stop` gives the guest's `sync`: `tart stop` is a hard stop, so what the guest has not written to
its disk is lost."""
HOST_ENV = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "TART_HOME")
"""The host variables a `tart` process gets: what it needs to find itself and its VMs, and nothing of serve's
own (keys, tokens). The `TART_REGISTRY_*` ones (a private registry's credentials) go with them."""
HOSTS_BEGIN = "# BEGIN airlie allow_list"
HOSTS_END = "# END airlie allow_list"
HOSTS_SCRIPT = (
    f'f=/etc/hosts; t=$(mktemp) && sed "/^{HOSTS_BEGIN}$/,/^{HOSTS_END}$/d" "$f" > "$t" && cat >> "$t" '
    '&& cat "$t" > "$f"; rc=$?; rm -f "$t"; exit $rc'
)
"""Run as root in the guest with the block on stdin: drops the old airlie block, appends the new one."""
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOST_NAME = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")


def tart_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in HOST_ENV or k.startswith("TART_REGISTRY_")}


def softnet_installed() -> bool:
    return shutil.which("softnet") is not None


async def _pump(stream: asyncio.StreamReader | None, into: bytearray, limit: int = OUTPUT_MAX) -> None:
    if stream is None:
        return
    while chunk := await stream.read(65536):
        into += chunk[: max(limit - len(into), 0)]


async def _feed(proc: asyncio.subprocess.Process, data: bytes) -> None:
    assert proc.stdin is not None
    with contextlib.suppress(OSError):  # the command ended without reading it all
        proc.stdin.write(data)
        await proc.stdin.drain()
    with contextlib.suppress(OSError):
        proc.stdin.close()


class RealRunner:
    async def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout_s: float, limit: int = OUTPUT_MAX
    ) -> tuple[int | None, bytes, bytes]:
        proc = await started(
            asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=tart_env(),
                start_new_session=True,  # its own process group: killpg reaches what it starts
            )
        )
        out, err = bytearray(), bytearray()
        work = asyncio.gather(
            _pump(proc.stdout, out, limit),
            _pump(proc.stderr, err, limit),
            *([_feed(proc, stdin)] if stdin is not None else []),
            proc.wait(),
        )
        try:
            await asyncio.wait_for(asyncio.shield(work), timeout=timeout_s)
        except TimeoutError:
            # kill the group, then let the pumps read what is left in the pipes (not `kill_group`: its own
            # reads would compete with them), so the partial output is kept
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(work, DRAIN_S)
            return None, bytes(out), bytes(err)
        except BaseException:  # cancelled: nothing it started may outlive its caller
            work.cancel()
            await kill_group(proc)
            raise
        return proc.returncode, bytes(out), bytes(err)


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
                env=tart_env(),
                start_new_session=True,
            )
        )
    return _Proc(proc)


async def dns_resolve(host: str) -> list[str]:
    """The IPv4 addresses of `host` as /32 CIDRs."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET)
    return sorted({f"{info[4][0]}/32" for info in infos})


def _text(out: bytes, err: bytes) -> str:
    return (out + err).decode("utf-8", "replace").strip()


def _missing(text: str) -> bool:
    return "does not exist" in text


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
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._run = run
        self._tart = tart
        self._spawn = spawn
        self._resolve = resolve
        self._log = log_dir / f"{name}.log"
        self._poll_s = poll_s
        self._sleep = sleep
        self._now = now
        self._proc: Spawned | None = None
        self._hosts: dict[str, list[str]] = {}  # allowed names -> addresses, as resolved for the last boot

    async def _tart_run(
        self, *args: str, stdin: bytes | None = None, timeout_s: float = 120.0, limit: int = OUTPUT_MAX
    ) -> tuple[int | None, bytes, bytes]:
        try:
            return await self._run([self._tart, *args], stdin, timeout_s, limit)
        except FileNotFoundError as e:
            raise BackendMissing(INSTALL_HINT) from e

    async def _checked(self, *args: str, timeout_s: float = 120.0, tolerate_missing: bool = False) -> None:
        """`tart <args>`, raising BoxUnavailable when it fails."""
        rc, out, err = await self._tart_run(*args, timeout_s=timeout_s)
        text = _text(out, err)
        if rc != 0 and not (tolerate_missing and _missing(text)):
            raise BoxUnavailable(f"tart {' '.join(args[:2])} failed: {text}")

    async def _info(self) -> dict[str, object] | None:
        rc, out, _ = await self._tart_run("get", self.name, "--format", "json", timeout_s=30.0)
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
        await self._checked("clone", image, self.name, timeout_s=3600.0)
        await self._checked(
            "set",
            self.name,
            "--cpu",
            str(cpus),
            "--memory",
            str(int(memory_gb * 1024)),
            "--disk-size",
            str(disk_gb),
        )

    async def _network_flags(self, network: Network) -> list[str]:
        """open: none. off: softnet blocks every IPv4 address (not `--net-host`: it reaches the host's own
        services on every interface). allow_list: the same block, then the allowed hosts' addresses and
        nothing else: no resolver and no gateway (each boot's /29 gives the gateway another address), so the
        guest finds the names in its /etc/hosts, written after boot from this same resolution. Softnet
        filters IPv4 only and sends no IPv6 itself (it lets a guest send "to globally routable IPv4
        addresses"), so IPv6 is not a way out."""
        if network.mode == "open":
            return []
        flags = ["--net-softnet-block=0.0.0.0/0"]
        if network.mode == "off":
            return flags
        cidrs: list[str] = []
        self._hosts = {}
        for entry in network.allow:  # a CIDR as it is, a host name resolved
            if "/" in entry:
                found = [entry]
            else:
                if not HOST_NAME.match(entry):
                    raise BoxUnavailable(f"allowed host {entry!r} is not a valid host name")
                try:
                    found = await self._resolve(entry)
                except Exception as e:  # a name that does not resolve is said, not dropped
                    raise BoxUnavailable(f"allowed host {entry} did not resolve: {e!r}") from e
                if not found:
                    raise BoxUnavailable(f"allowed host {entry} did not resolve: no IPv4 address")
                self._hosts[entry] = found
            for cidr in found:
                if cidr not in cidrs:
                    cidrs.append(cidr)
        return [*flags, f"--net-softnet-allow={','.join(cidrs)}"] if cidrs else flags

    async def _write_hosts(self) -> None:
        """The resolved names into the guest's /etc/hosts between airlie's marker lines (replaced, never
        appended to). IPv4 only: the guest has no global IPv6, softnet filters IPv4 only. A CDN that rotates
        its addresses mid-session needs a VM restart (a new resolution)."""
        lines = [f"{c.split('/')[0]} {host}" for host, found in self._hosts.items() for c in found]
        if not lines:
            return
        block = "\n".join([HOSTS_BEGIN, *lines, HOSTS_END]) + "\n"
        res = await self.exec(
            ["sudo", "-n", "sh", "-c", HOSTS_SCRIPT],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            stdin=block.encode(),
            timeout_s=30.0,
        )
        if res.rc != 0:
            raise BoxUnavailable(
                f"could not write the allowed hosts into {self.name}'s /etc/hosts: {res.output}"
            )

    def _log_tail(self) -> str:
        try:
            return self._log.read_text(errors="replace")[-LOG_TAIL_CHARS:].strip()
        except OSError:
            return "(no log)"

    async def boot(self, *, network: Network, timeout_s: float) -> None:
        argv = [self._tart, "run", "--no-graphics", *await self._network_flags(network), self.name]
        try:
            proc = self._proc = await self._spawn(argv, self._log)
        except FileNotFoundError as e:
            raise BackendMissing(INSTALL_HINT) from e
        try:
            await self._answering(proc, timeout_s)
            if network.mode == "allow_list":
                await self._write_hosts()
        except BaseException:  # failed, timed out or cancelled (a Stop): no `tart run` outlives its boot
            self._proc = None
            await proc.kill()
            raise

    async def _answering(self, proc: Spawned, timeout_s: float) -> None:
        deadline = self._now() + timeout_s
        while True:
            left = deadline - self._now()
            rc, _out, _err = await self._tart_run(
                "exec", self.name, "true", timeout_s=max(1.0, min(self._poll_s + 10, left))
            )
            if rc == 0:
                return
            if proc.returncode is not None:  # tart run ended: nothing will ever answer
                raise BoxUnavailable(
                    f"tart run exited (rc {proc.returncode}) before {self.name} answered; "
                    f"tart run log ({self._log}):\n{self._log_tail()}"
                )
            if self._now() >= deadline:
                raise BoxUnavailable(
                    f"{self.name} did not answer within {timeout_s:g}s; "
                    f"tart run log ({self._log}):\n{self._log_tail()}"
                )
            await self._sleep(self._poll_s)

    def _exec_args(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: str | None, *, stdin: bool
    ) -> list[str]:
        script = f"cd {shlex.quote(cwd)} && " if cwd else ""
        script += 'exec "$@"'
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
        args = self._exec_args(argv, env, cwd, stdin=stdin is not None)
        rc, out, err = await self._tart_run(*args, stdin=stdin, timeout_s=timeout_s)
        text = (out + err).decode("utf-8", "replace")
        if rc is None:
            note = f"error: command timed out after {timeout_s:g}s"
            return ExecResult(None, f"{text}\n{note}" if text else note)
        return ExecResult(rc, text)

    async def exec_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        stdin: bytes | None = None,
        timeout_s: float = 120.0,
        max_bytes: int = OUTPUT_MAX,
    ) -> tuple[int | None, bytes]:
        """stdout on success; when the command fails (or times out) its stderr instead. The runner keeps one
        byte more than `max_bytes`, so a stream over it is told apart from one exactly at it and refused."""
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
        args = self._exec_args(argv, env, cwd, stdin=stdin is not None)
        rc, out, err = await self._tart_run(*args, stdin=stdin, timeout_s=timeout_s, limit=max_bytes + 1)
        if len(out) > max_bytes or len(err) > max_bytes:
            return None, f"output over {max_bytes} bytes".encode()
        return rc, out if rc == 0 else err

    async def stop(self) -> None:
        """`sync` in the guest first (best effort: a guest that does not answer is stopped anyway), as `tart
        stop` loses what the guest has not written to its disk yet."""
        await self._tart_run(*self._exec_args(["sync"], {}, None, stdin=False), timeout_s=SYNC_S)
        await self._checked("stop", self.name, timeout_s=STOP_WAIT_S + 30, tolerate_missing=True)
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), STOP_WAIT_S)
            except TimeoutError:
                await proc.kill()

    async def rename(self, new_name: str) -> None:
        await self._checked("rename", self.name, new_name)
        self.name = new_name

    async def delete(self) -> None:
        if await self.running():
            await self.stop()
        await self._checked("delete", self.name, tolerate_missing=True)
