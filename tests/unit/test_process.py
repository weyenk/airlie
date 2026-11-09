"""EngineProcess: start/stop/status over an injected ProcessRunner and HTTP prober; no real server."""

from pathlib import Path

import httpx
import pytest
import yaml

from airlie.config import ModelRef, load_config
from airlie.engine.process import EngineProcess, HTTPProber, ProcessRunner
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
        models: list[list[str]],
        loaded: list[list[str]] | None = None,
        failures: list[str] | None = None,
    ) -> None:
        self._health = list(health)
        self._models = list(models)
        self._loaded = list(loaded or [])
        self._failures = failures or []

    async def health_ok(self) -> bool:
        return self._health.pop(0) if self._health else True

    async def model_ids(self) -> list[str]:
        return self._models.pop(0) if self._models else []

    async def loaded_model_ids(self) -> list[str]:
        if len(self._loaded) > 1:
            return self._loaded.pop(0)
        return self._loaded[0] if self._loaded else []

    async def preload_failures(self) -> list[str]:
        return self._failures


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
    argv, log, _ = runner.spawned[0]
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

    def spawn_and_die(argv, log_path, cwd, env=None):
        pid = orig(argv, log_path, cwd, env)
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
    argv, _, _ = runner.spawned[0]
    assert argv[:3] == ["llama-server", "-m", "/hf/ggml-org/Kev-4B-GGUF/d9/Kev-4B-Q4_K_M.gguf"]
    assert h.ok and h.models == {"kev": "ready"}
    assert not p.registry_path.exists()  # no vllm-mlx registry for a llama_server engine


async def test_a_model_with_an_mtp_head_is_served_from_a_directory_built_for_it(
    tmp_path: Path,
) -> None:
    mx = pytest.importorskip("mlx.core", reason="mlx installs on macOS only")
    hf = tmp_path / "hf"
    (hf / "org/base/r1").mkdir(parents=True)
    (hf / "org/base/r1/config.json").write_text("{}")
    (hf / "org/base-MTP/h1").mkdir(parents=True)
    mx.save_safetensors(str(hf / "org/base-MTP/h1/model.safetensors"), {"fc.weight": mx.ones((2,))})
    c = cfg(tmp_path)
    c.models["base"].mtp = ModelRef(repo="org/base-MTP", revision="h1")
    runner = FakeRunner()
    p = EngineProcess(
        c,
        "main",
        state_dir=tmp_path / "state",
        runner=runner,
        prober=FakeProber(health=[True], models=[["base", "small"]]),
        snapshot_dir=lambda m: hf / m.repo / m.revision,
        poll_s=0,
    )
    await p.start(timeout_s=5)
    argv, _, _ = runner.spawned[0]
    base = yaml.safe_load(Path(argv[5]).read_text())["models"][0]
    built = tmp_path / "state" / "engines" / "main" / "models" / "base-r1-h1"
    assert base["path"] == str(built) and base["enable_mtp"] is True
    assert (built / "config.json").is_symlink() and (built / "mtp" / "weights.safetensors").is_file()
    assert "--enable-mtp" in argv


def mlx_cfg(tmp_path: Path):
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


def mlx_proc(tmp_path: Path, runner: FakeRunner, prober: FakeProber) -> EngineProcess:
    return EngineProcess(
        mlx_cfg(tmp_path),
        "main",
        state_dir=tmp_path / "state",
        runner=runner,
        prober=prober,
        snapshot_dir=lambda m: Path(f"/hf/{m.repo}/{m.revision}"),
        poll_s=0,
    )


async def test_mlx_vlm_engine_starts_with_its_command_and_env_and_waits_for_loaded(tmp_path: Path) -> None:
    prober = FakeProber(
        health=[True] * 3, models=[], loaded=[["/models/other"], ["/models/other"], ["/models/base"]]
    )
    runner = FakeRunner()
    p = mlx_proc(tmp_path, runner, prober)
    h = await p.start(timeout_s=5)  # listed-but-not-loaded twice, then loaded under the exact --model path
    argv, _, env = runner.spawned[0]
    assert argv[:5] == ["python", "-m", "mlx_vlm.server", "--model", "/models/base"]
    assert env is not None and env["APC_ENABLED"] == "1" and env["APC_DISK_ENABLED"] == "0"
    assert h.ok and h.models == {"base": "ready"}
    assert not (tmp_path / "state" / "engines" / "main" / "models.yaml").exists()  # no registry file


async def test_mlx_vlm_engine_is_not_ready_while_another_name_is_loaded(tmp_path: Path) -> None:
    prober = FakeProber(health=[True], models=[], loaded=[["base"], ["/models/other"]])
    p = mlx_proc(tmp_path, FakeRunner(), prober)
    with pytest.raises(EngineError, match=r"base: loading"):
        await p.start(timeout_s=0.01)


async def test_mlx_vlm_engine_start_fails_fast_on_a_preload_failure(tmp_path: Path) -> None:
    prober = FakeProber(health=[True], models=[], loaded=[[]], failures=["/models/base: out of memory"])
    p = mlx_proc(tmp_path, FakeRunner(), prober)
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
