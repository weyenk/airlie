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
