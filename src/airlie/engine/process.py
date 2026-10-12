"""EngineProcess: start, stop and inspect the vllm-mlx server that serves one `vllm_mlx` engine entry.

All OS interaction goes through `ProcessRunner` and `Prober`, so the logic is unit-tested without a server."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
import psutil

from airlie.config import AirlieConfig, ModelSpec
from airlie.engine.registry import build_registry, build_serve_command, write_registry
from airlie.engine.types import EngineError, ModelState


class ProcessRunner(Protocol):
    def spawn(self, argv: list[str], log_path: Path, cwd: Path) -> int: ...
    def is_alive(self, pid: int) -> bool: ...
    def terminate(self, pid: int, signal: str) -> None: ...
    def port_holder(self, port: int) -> int | None: ...


class Prober(Protocol):
    async def health_ok(self) -> bool: ...
    async def model_ids(self) -> list[str]: ...


@dataclass(frozen=True)
class ProcessHealth:
    ok: bool
    models: dict[str, ModelState]
    pid: int | None
    detail: str = ""


class RealRunner:
    """Spawns `uv run vllm-mlx …` detached from the CLI so it survives `task up` returning."""

    def spawn(self, argv: list[str], log_path: Path, cwd: Path) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                ["uv", "run", "--quiet", *argv],
                cwd=cwd,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        return proc.pid

    def is_alive(self, pid: int) -> bool:
        try:
            p = psutil.Process(pid)
            return bool(p.is_running() and p.status() != psutil.STATUS_ZOMBIE)
        except psutil.Error:
            return False

    def terminate(self, pid: int, sig: str) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(pid), signal.SIGTERM if sig == "TERM" else signal.SIGKILL)

    def port_holder(self, port: int) -> int | None:
        try:
            for c in psutil.net_connections(kind="inet"):
                if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
                    return int(c.pid) if c.pid is not None else None
        except psutil.AccessDenied:
            return None
        return None


class HTTPProber:
    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self._root = base_url.removesuffix("/v1").rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def health_ok(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0, headers=self._headers) as c:
                r = await c.get(f"{self._root}/health")
                return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def model_ids(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=3.0, headers=self._headers) as c:
                r = await c.get(f"{self._root}/v1/models")
                if r.status_code != 200:
                    return []
                return [str(m.get("id")) for m in r.json().get("data", [])]
        except (httpx.HTTPError, ValueError):
            return []


def hf_snapshot_dir(m: ModelSpec, hf_cache: Path | None = None) -> Path:
    cache = hf_cache or Path("~/.cache/huggingface/hub").expanduser()
    return cache / f"models--{m.repo.replace('/', '--')}" / "snapshots" / m.revision


class EngineProcess:
    def __init__(
        self,
        cfg: AirlieConfig,
        engine_name: str,
        *,
        state_dir: Path,
        runner: ProcessRunner | None = None,
        prober: Prober | None = None,
        snapshot_dir: Callable[[ModelSpec], Path] = hf_snapshot_dir,
        poll_s: float = 1.0,
        cwd: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.name = engine_name
        self.spec = cfg.engines[engine_name]
        self.dir = state_dir / "engines" / engine_name
        self.runner = runner or RealRunner()
        self.prober = prober or HTTPProber(self.spec.base_url or "", self.spec.api_key)
        self.snapshot_dir = snapshot_dir
        self.poll_s = poll_s
        self.cwd = cwd or Path.cwd()

    # ---- files ----
    @property
    def pidfile(self) -> Path:
        return self.dir / "pid"

    @property
    def log_path(self) -> Path:
        return self.dir / "server.log"

    @property
    def registry_path(self) -> Path:
        return self.dir / "models.yaml"

    def pid(self) -> int | None:
        try:
            pid = int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None
        if self.runner.is_alive(pid):
            return pid
        self.pidfile.unlink(missing_ok=True)
        return None

    # ---- lifecycle ----
    async def start(self, timeout_s: float = 600.0) -> ProcessHealth:
        if (pid := self.pid()) is not None:
            raise EngineError(f"engine '{self.name}' is already running (pid {pid}); use restart")
        if (holder := self.runner.port_holder(self.spec.port)) is not None:
            raise EngineError(
                f"port {self.spec.port} is held by pid {holder}, not by airlie; "
                f"stop it or change engines.{self.name}.port"
            )
        registry = build_registry(self.cfg, self.name, self.snapshot_dir)
        write_registry(registry, self.registry_path)
        argv = build_serve_command(self.cfg, self.name, self.registry_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        pid = self.runner.spawn(argv, self.log_path, self.cwd)
        self.pidfile.write_text(f"{pid}\n")
        return await self._wait_ready(pid, timeout_s)

    async def _wait_ready(self, pid: int, timeout_s: float) -> ProcessHealth:
        deadline = time.monotonic() + timeout_s
        preload = [m for m in self.spec.models if self.cfg.models[m].preload]
        last: ProcessHealth = ProcessHealth(False, {}, pid, "starting")
        while True:
            if not self.runner.is_alive(pid):
                self.pidfile.unlink(missing_ok=True)
                raise EngineError(f"engine '{self.name}' exited during startup; see {self.log_path}")
            last = await self._probe(pid)
            if last.ok and all(last.models.get(m) == "ready" for m in preload):
                return last
            if time.monotonic() >= deadline:
                pending = ", ".join(
                    f"{m}: {last.models.get(m, 'missing')}" for m in preload if last.models.get(m) != "ready"
                )
                raise EngineError(
                    f"engine '{self.name}' not ready after {timeout_s:.0f}s ({pending}); see {self.log_path}"
                )
            await asyncio.sleep(self.poll_s)

    async def _probe(self, pid: int | None) -> ProcessHealth:
        ok = await self.prober.health_ok()
        ids = set(await self.prober.model_ids()) if ok else set()
        models: dict[str, ModelState] = {}
        for m in self.spec.models:
            if m not in ids:
                models[m] = "loading" if ok else "missing"
            elif self.cfg.models[m].preload:
                models[m] = "ready"  # vllm-mlx preloads before /health answers (see server.log)
            else:
                models[m] = "lazy"  # registered; the registry loads it on first request
        return ProcessHealth(ok, models, pid, "healthy" if ok else "health endpoint not answering")

    async def status(self) -> ProcessHealth:
        pid = self.pid()
        if pid is None:
            return ProcessHealth(False, dict.fromkeys(self.spec.models, "missing"), None, "not running")
        return await self._probe(pid)

    async def stop(self, grace_s: float = 15.0) -> None:
        pid = self.pid()
        if pid is None:
            return
        self.runner.terminate(pid, "TERM")
        deadline = time.monotonic() + grace_s
        while self.runner.is_alive(pid) and time.monotonic() < deadline:
            await asyncio.sleep(min(self.poll_s, 0.2))
        if self.runner.is_alive(pid):
            self.runner.terminate(pid, "KILL")
        self.pidfile.unlink(missing_ok=True)

    def tail_log(self, n: int = 50) -> str:
        try:
            lines = self.log_path.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])


def default_state_dir(cfg: AirlieConfig) -> Path:
    return cfg.observability.state_dir.expanduser()
