"""EngineProcess: start/stop/status over an injected ProcessRunner and HTTP prober; no real server."""

from pathlib import Path

import pytest
import yaml

from airlie.config import load_config
from airlie.engine.process import EngineProcess, ProcessRunner
from airlie.engine.types import EngineError


def cfg(tmp_path: Path):
    data = {
        "version": 1,
        "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["base", "small"]}},
        "models": {
            "base": {"repo": "org/base", "revision": "r1", "estimated_memory_gb": 16},
            "small": {"repo": "org/small", "revision": "r2", "estimated_memory_gb": 3},
        },
        "roles": {
            r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


class FakeRunner(ProcessRunner):
    def __init__(self, *, port_holder: int | None = None) -> None:
        self.spawned: list[tuple[list[str], Path]] = []
        self.alive: set[int] = set()
        self.killed: list[tuple[int, str]] = []
        self._port_holder = port_holder
        self.next_pid = 4242

    def spawn(self, argv: list[str], log_path: Path, cwd: Path) -> int:
        self.spawned.append((argv, log_path))
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
    """Scripted answers for GET /health and GET /v1/models."""

    def __init__(self, health: list[bool], models: list[list[str]]) -> None:
        self._health = list(health)
        self._models = list(models)

    async def health_ok(self) -> bool:
        return self._health.pop(0) if self._health else True

    async def model_ids(self) -> list[str]:
        return self._models.pop(0) if self._models else []


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


async def test_start_writes_registry_spawns_and_waits_for_preloaded_models(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[False, True], models=[["base"], ["base", "small"]]))
    h = await p.start(timeout_s=5)
    argv, log = runner.spawned[0]
    assert argv[:4] == ["python", "-m", "airlie.engine.vllm_mlx_serve", "serve"]
    assert Path(argv[5]).is_file()  # the generated registry
    assert yaml.safe_load(Path(argv[5]).read_text())["models"][1]["name"] == "small"
    assert log == tmp_path / "state" / "engines" / "main" / "server.log"
    assert (tmp_path / "state" / "engines" / "main" / "pid").read_text().strip() == "4242"
    assert h.ok and h.models == {"base": "ready", "small": "ready"}


async def test_start_refuses_when_already_running(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[True], models=[["base", "small"]]))
    await p.start(timeout_s=5)
    with pytest.raises(EngineError, match=r"already running \(pid 4242\)"):
        await p.start(timeout_s=5)


async def test_start_refuses_busy_port_naming_holder(tmp_path: Path) -> None:
    p = proc(tmp_path, FakeRunner(port_holder=999), FakeProber(health=[], models=[]))
    with pytest.raises(EngineError, match=r"port 8080 is held by pid 999"):
        await p.start(timeout_s=5)


async def test_start_times_out_with_loading_state(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[True], models=[["base"]] * 50))
    with pytest.raises(EngineError, match=r"small: loading"):
        await p.start(timeout_s=0.01)


async def test_start_fails_fast_if_process_dies(tmp_path: Path) -> None:
    runner = FakeRunner()
    prober = FakeProber(health=[False] * 50, models=[])
    p = proc(tmp_path, runner, prober)

    orig = runner.spawn

    def spawn_and_die(argv, log_path, cwd):
        pid = orig(argv, log_path, cwd)
        runner.alive.discard(pid)
        return pid

    runner.spawn = spawn_and_die  # type: ignore[method-assign]
    with pytest.raises(EngineError, match=r"exited during startup.*server\.log"):
        await p.start(timeout_s=5)


async def test_status_reports_loading_model(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[True, True], models=[["base", "small"], ["base"]]))
    await p.start(timeout_s=5)
    h = await p.status()
    assert h.ok is True
    assert h.models == {"base": "ready", "small": "loading"}
    assert h.pid == 4242


async def test_non_preloaded_model_is_lazy_not_ready(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[True, True], models=[["base", "small"], ["base", "small"]]))
    p.cfg.models["small"].preload = False
    await p.start(timeout_s=5)
    assert (await p.status()).models == {"base": "ready", "small": "lazy"}


async def test_status_when_not_running(tmp_path: Path) -> None:
    p = proc(tmp_path, FakeRunner(), FakeProber(health=[], models=[]))
    h = await p.status()
    assert h.ok is False and h.pid is None and "not running" in h.detail


async def test_stop_terminates_then_kills_and_clears_pidfile(tmp_path: Path) -> None:
    runner = FakeRunner()
    p = proc(tmp_path, runner, FakeProber(health=[True], models=[["base", "small"]]))
    await p.start(timeout_s=5)
    await p.stop(grace_s=0)
    assert runner.killed[0] == (4242, "TERM")
    assert not (tmp_path / "state" / "engines" / "main" / "pid").exists()
    assert (await p.status()).ok is False


def llama_cfg(tmp_path: Path):
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["base"]},
            "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"]},
        },
        "models": {
            "base": {"repo": "org/base", "revision": "r1"},
            "kev": {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9", "file": "Kev-4B-Q4_K_M.gguf"},
        },
        "roles": {
            r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


async def test_llama_server_engine_starts_with_the_gguf_and_waits_for_its_alias(tmp_path: Path) -> None:
    runner = FakeRunner()
    prober = FakeProber(health=[False, True], models=[[], ["kev"]])
    p = EngineProcess(
        llama_cfg(tmp_path),
        "decision",
        state_dir=tmp_path / "state",
        runner=runner,
        prober=prober,
        snapshot_dir=lambda m: Path(f"/hf/{m.repo}/{m.revision}"),
        poll_s=0.0,
    )
    h = await p.start(timeout_s=5)
    argv, _ = runner.spawned[0]
    assert argv[:3] == ["llama-server", "-m", "/hf/ggml-org/Kev-4B-GGUF/d9/Kev-4B-Q4_K_M.gguf"]
    assert h.ok and h.models == {"kev": "ready"}
    assert not p.registry_path.exists()  # no vllm-mlx registry for a llama_server engine
