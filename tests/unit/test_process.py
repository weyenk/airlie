"""EngineProcess: start/stop/status over an injected ProcessRunner and HTTP prober; no real server."""

from pathlib import Path

import httpx
import pytest
import yaml

from airlie.config import load_config
from airlie.engine.process import EngineProcess, HTTPProber, ProcessRunner
from airlie.engine.types import EngineError


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


def cfg(tmp_path: Path):
    data = {
        "version": 1,
        "engines": {"main": {"kind": "mlx_vlm", "port": 8080, "model": "base", "prefix_cache": True}},
        "models": {
            "base": {"repo": "org/base", "revision": "r1", "path": "/models/base", "estimated_memory_gb": 16}
        },
        "roles": {
            r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def proc(tmp_path: Path, runner: FakeRunner, prober: FakeProber) -> EngineProcess:
    return EngineProcess(
        cfg(tmp_path),
        "main",
        state_dir=tmp_path / "state",
        runner=runner,
        prober=prober,
        snapshot_dir=lambda m: Path(f"/hf/{m.repo}/{m.revision}"),
        poll_s=0,
    )


async def test_start_refuses_when_already_running(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[True], loaded=[["/models/base"]]))
    await p.start(timeout_s=5)
    with pytest.raises(EngineError, match=r"already running \(pid 4242\)"):
        await p.start(timeout_s=5)


async def test_start_refuses_busy_port_naming_holder(tmp_path: Path) -> None:
    p = proc(tmp_path, FakeRunner(port_holder=999), FakeProber(health=[]))
    with pytest.raises(EngineError, match=r"port 8080 is held by pid 999"):
        await p.start(timeout_s=5)


async def test_start_fails_fast_if_process_dies(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[False] * 50))

    orig = runner.spawn

    def spawn_and_die(argv, log_path, cwd, env=None):
        pid = orig(argv, log_path, cwd, env)
        runner.alive.discard(pid)
        return pid

    runner.spawn = spawn_and_die  # type: ignore[method-assign]
    with pytest.raises(EngineError, match=r"exited during startup.*server\.log"):
        await p.start(timeout_s=5)


async def test_status_reports_a_model_not_yet_loaded_as_loading(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[True, True], loaded=[["/models/base"], []]))
    await p.start(timeout_s=5)
    h = await p.status()
    assert h.ok is True
    assert h.models == {"base": "loading"}
    assert h.pid == 4242


async def test_status_when_not_running(tmp_path: Path) -> None:
    p = proc(tmp_path, FakeRunner(), FakeProber(health=[]))
    h = await p.status()
    assert h.ok is False and h.pid is None and "not running" in h.detail


async def test_stop_terminates_then_kills_and_clears_pidfile(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[True], loaded=[["/models/base"]]))
    await p.start(timeout_s=5)
    await p.stop(grace_s=0)
    assert runner.killed[0] == (4242, "TERM")
    assert not (tmp_path / "state" / "engines" / "main" / "pid").exists()
    assert (await p.status()).ok is False


async def test_mlx_vlm_engine_starts_with_its_command_and_env_and_waits_for_loaded(tmp_path: Path) -> None:
    prober = FakeProber(health=[True] * 3, loaded=[["/models/other"], ["/models/other"], ["/models/base"]])
    runner = FakeRunner()
    p = proc(tmp_path, runner, prober)
    h = await p.start(timeout_s=5)  # listed-but-not-loaded twice, then loaded under the exact --model path
    argv, _, env = runner.spawned[0]
    assert argv[:5] == ["python", "-m", "mlx_vlm.server", "--model", "/models/base"]
    assert env is not None and env["APC_ENABLED"] == "1" and env["APC_DISK_ENABLED"] == "0"
    assert h.ok and h.models == {"base": "ready"}
    assert not (tmp_path / "state" / "engines" / "main" / "models.yaml").exists()  # no registry file


async def test_mlx_vlm_engine_is_not_ready_while_another_name_is_loaded(tmp_path: Path) -> None:
    prober = FakeProber(health=[True], loaded=[["base"], ["/models/other"]])
    p = proc(tmp_path, FakeRunner(), prober)
    with pytest.raises(EngineError, match=r"base: loading"):
        await p.start(timeout_s=0.01)


async def test_mlx_vlm_engine_start_fails_fast_on_a_preload_failure(tmp_path: Path) -> None:
    prober = FakeProber(health=[True], loaded=[[]], failures=["/models/base: out of memory"])
    p = proc(tmp_path, FakeRunner(), prober)
    with pytest.raises(
        EngineError, match=r"failed to load its model: /models/base: out of memory.*server\.log"
    ):
        await p.start(timeout_s=60)


async def test_prober_sends_the_api_key_to_health_models_and_metrics() -> None:
    fixtures = Path(__file__).parent.parent / "fixtures" / "mlx_vlm"
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers.get("authorization")
        name = {"/health": "health.json", "/v1/models": "models.json", "/v1/metrics": "metrics.json"}[
            request.url.path
        ]
        return httpx.Response(200, content=(fixtures / name).read_bytes())

    prober = HTTPProber("http://e/v1", api_key="k", transport=httpx.MockTransport(handler))
    assert await prober.health_ok()
    assert await prober.loaded_model_ids() == ["/models/small"]
    assert await prober.preload_failures() == []
    assert seen == {"/health": "Bearer k", "/v1/models": "Bearer k", "/v1/metrics": "Bearer k"}


async def test_prober_reads_preload_failures_in_each_shape() -> None:
    def prober(body: object, status: int = 200) -> HTTPProber:
        return HTTPProber(
            "http://e/v1",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(status, json={"server": {"preload_failures": body}})
            ),
        )

    real = {"text_generation": {"model": "/models/base", "label": "model", "error": "out of memory"}}
    assert await prober(real).preload_failures() == ["/models/base: out of memory"]
    assert await prober(["boom"]).preload_failures() == ["boom"]
    assert await prober(2).preload_failures() == ["2 preload failure(s)"]
    assert await prober({}).preload_failures() == []
    assert await prober(real, status=500).preload_failures() == []
