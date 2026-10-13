"""`airlie engine up|down|status|logs`: the CLI builds an EngineProcess per vllm_mlx engine and renders it."""

from pathlib import Path

import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.engine.process import ProcessHealth

runner = CliRunner()


def write_cfg(tmp_path: Path) -> Path:
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["base"]},
            "ext": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["small"]},
        },
        "models": {
            "base": {"repo": "org/base", "revision": "r1", "estimated_memory_gb": 16},
            "small": {"repo": "org/small", "revision": "r2", "estimated_memory_gb": 3},
        },
        "roles": {
            r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (tmp_path / "p.md").write_text("x")
    p = tmp_path / "airlie.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


class StubProcess:
    calls: list[str] = []

    def __init__(self, cfg, name, **kw) -> None:
        self.name = name
        self.log_path = Path("/tmp/x.log")

    async def start(self, timeout_s: float = 600.0) -> ProcessHealth:
        StubProcess.calls.append(f"start:{self.name}")
        return ProcessHealth(True, {"base": "ready"}, 123, "healthy")

    async def stop(self, grace_s: float = 15.0) -> None:
        StubProcess.calls.append(f"stop:{self.name}")

    async def status(self) -> ProcessHealth:
        StubProcess.calls.append(f"status:{self.name}")
        return ProcessHealth(True, {"base": "loading"}, 123, "healthy")

    def tail_log(self, n: int = 50) -> str:
        return "line1\nline2"


def test_up_starts_only_managed_engines(tmp_path: Path, monkeypatch) -> None:
    StubProcess.calls = []
    monkeypatch.setattr(cli, "_engine_process", lambda cfg, name: StubProcess(cfg, name))
    r = runner.invoke(cli.app, ["engine", "up", "--config", str(write_cfg(tmp_path))])
    assert r.exit_code == 0, r.output
    assert StubProcess.calls == ["start:main"]
    assert "main" in r.output and "ready" in r.output


def test_status_shows_loading_models(tmp_path: Path, monkeypatch) -> None:
    StubProcess.calls = []
    monkeypatch.setattr(cli, "_engine_process", lambda cfg, name: StubProcess(cfg, name))
    r = runner.invoke(cli.app, ["engine", "status", "--config", str(write_cfg(tmp_path))])
    assert r.exit_code == 0, r.output
    assert "loading" in r.output and "123" in r.output


def test_down_stops_managed_engines(tmp_path: Path, monkeypatch) -> None:
    StubProcess.calls = []
    monkeypatch.setattr(cli, "_engine_process", lambda cfg, name: StubProcess(cfg, name))
    r = runner.invoke(cli.app, ["engine", "down", "--config", str(write_cfg(tmp_path))])
    assert r.exit_code == 0, r.output
    assert StubProcess.calls == ["stop:main"]


def test_logs_prints_tail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_engine_process", lambda cfg, name: StubProcess(cfg, name))
    r = runner.invoke(cli.app, ["engine", "logs", "-n", "2", "--config", str(write_cfg(tmp_path))])
    assert r.exit_code == 0, r.output
    assert "line1" in r.output and "line2" in r.output


def test_up_skips_an_engine_that_is_already_running_and_starts_the_rest(tmp_path: Path, monkeypatch) -> None:
    from airlie.engine.types import EngineError

    class Running(StubProcess):
        async def start(self, timeout_s: float = 600.0) -> ProcessHealth:
            StubProcess.calls.append(f"start:{self.name}")
            raise EngineError(f"engine '{self.name}' is already running (pid 7); use restart")

        async def status(self) -> ProcessHealth:
            return ProcessHealth(True, {"base": "ready"}, 7, "healthy")

    StubProcess.calls = []
    monkeypatch.setattr(
        cli,
        "_engine_process",
        lambda cfg, name: Running(cfg, name) if name == "main" else StubProcess(cfg, name),
    )
    p = write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text())
    data["engines"]["decision"] = {"kind": "llama_server", "port": 8081, "models": ["kev"]}
    data["models"]["kev"] = {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9", "file": "k.gguf"}
    p.write_text(yaml.safe_dump(data))
    r = runner.invoke(cli.app, ["engine", "up", "--config", str(p)])
    assert r.exit_code == 0, r.output
    assert sorted(StubProcess.calls) == ["start:decision", "start:main"]  # yaml dump sorts engine names
    assert "already running" in r.output


def test_engine_up_and_restart_take_a_topology_profile(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []

    def make(cfg, name):  # type: ignore[no-untyped-def]
        seen.append(cfg.topology.kind)
        return StubProcess(cfg, name)

    StubProcess.calls = []
    monkeypatch.setattr(cli, "_engine_process", make)
    p = write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text())
    data["topology"] = {"kind": "teams", "profiles": {"fork": {"engines": {"main": {"max_kv_size": 4096}}}}}
    p.write_text(yaml.safe_dump(data))
    r = runner.invoke(cli.app, ["engine", "up", "--config", str(p), "--topology", "fork"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli.app, ["engine", "restart", "--config", str(p), "--topology", "fork"])
    assert r.exit_code == 0, r.output
    assert seen and set(seen) == {"fork"}
