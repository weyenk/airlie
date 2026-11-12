"""Fakes for EngineProcess: a runner that spawns nothing and a prober with scripted answers."""

from pathlib import Path

from airlie.engine.process import ProcessRunner


class FakeRunner(ProcessRunner):
    def __init__(self, *, port_holder: int | None = None) -> None:
        self.spawned: list[tuple[list[str], Path, dict[str, str] | None]] = []
        self.alive: set[int] = set()
        self.killed: list[tuple[int, str]] = []
        self._port_holder = port_holder
        self.next_pid = 4242

    def spawn(self, argv: list[str], log_path: Path, cwd: Path, env: dict[str, str] | None = None) -> int:
        self.spawned.append((argv, log_path, env))
        pid = self.next_pid
        self.alive.add(pid)
        return pid

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def terminate(self, pid: int, signal: str) -> None:
        self.killed.append((pid, signal))
        self.alive.discard(pid)

    def port_holder(self, port: int) -> int | None:
        return self._port_holder


class FakeProber:
    """Scripted answers for GET /health, GET /v1/models and /v1/metrics."""

    def __init__(
        self,
        health: list[bool],
        loaded: list[list[str]] | None = None,
        failures: list[str] | None = None,
    ) -> None:
        self._health = list(health)
        self._loaded = list(loaded or [])
        self._failures = failures or []

    async def health_ok(self) -> bool:
        return self._health.pop(0) if self._health else True

    async def loaded_model_ids(self) -> list[str]:
        if len(self._loaded) > 1:
            return self._loaded.pop(0)
        return self._loaded[0] if self._loaded else []

    async def preload_failures(self) -> list[str]:
        return self._failures
