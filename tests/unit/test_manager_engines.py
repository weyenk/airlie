"""Engines as manager resources: download before start, adopt a running engine, a failed start says why.
Fake runner, prober and downloader; no process, no network."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import yaml

from airlie.config import AirlieConfig, load_config
from airlie.engine.process import EngineProcess
from airlie.engine.types import EngineError
from airlie.manager.core import Manager, State
from airlie.manager.engines import EngineResource, engine_resources
from airlie.models_download import Downloader, DownloadProgress
from airlie.serve.bridge.clock import FakeClock
from tests.support.process import FakeProber, FakeRunner

Event = tuple[str, str]
PROGRESS = DownloadProgress(5, 10, 1.0, 5.0)


def make_cfg(tmp_path: Path) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": {"main": {"kind": "mlx_vlm", "port": 8080, "model": "base", "lifecycle": "on_demand"}},
        "models": {
            "base": {
                "repo": "org/base",
                "revision": "r1",
                "path": "/models/base",
                "estimated_memory_gb": 16,
                "mtp": {"repo": "org/head", "revision": "m1", "estimated_memory_gb": 1},
            }
        },
        "roles": {
            r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


class FakeDownloader:
    def __init__(self, events: list[Event]) -> None:
        self.events = events

    async def ensure(
        self, repo: str, revision: str, on_progress: Callable[[DownloadProgress], None] = lambda p: None
    ) -> Path:
        self.events.append(("download", repo))
        on_progress(PROGRESS)
        return Path("/hf") / repo


def resource(
    tmp_path: Path,
    runner: FakeRunner,
    prober: FakeProber,
    events: list[Event],
    on_disk: set[str],
    start_timeout_s: float | None = None,
) -> EngineResource:
    cfg = make_cfg(tmp_path)
    if start_timeout_s is not None:
        cfg.manager.start_timeout_s = start_timeout_s
    original = runner.spawn

    def spawn(argv: list[str], log_path: Path, cwd: Path, env: dict[str, str] | None = None) -> int:
        events.append(("spawn", ""))
        return original(argv, log_path, cwd, env)

    runner.spawn = spawn  # type: ignore[method-assign]
    process = EngineProcess(
        cfg,
        "main",
        state_dir=tmp_path / "state",
        runner=runner,
        prober=prober,
        snapshot_dir=lambda m: Path(f"/hf/{m.repo}/{m.revision}"),
        poll_s=0,
    )
    return EngineResource(
        cfg,
        "main",
        process=process,
        downloader=cast(Downloader, FakeDownloader(events)),
        on_disk=lambda repo, rev: repo in on_disk,
    )


async def test_a_missing_model_downloads_before_start(tmp_path: Path) -> None:
    events: list[Event] = []
    reports: list[tuple[State, str, DownloadProgress | None]] = []
    res = resource(
        tmp_path,
        FakeRunner(),
        FakeProber(health=[True], loaded=[["/models/base"]]),
        events,
        on_disk={"org/head"},  # the body is missing, the head is there
    )
    await res.start(lambda s, d, p: reports.append((s, d, p)))
    assert events == [("download", "org/base"), ("spawn", "")]
    assert [s for s, _, _ in reports] == ["downloading", "downloading", "starting"]
    assert reports[1][2] == PROGRESS


async def test_nothing_downloads_when_the_files_are_on_disk(tmp_path: Path) -> None:
    events: list[Event] = []
    res = resource(
        tmp_path,
        FakeRunner(),
        FakeProber(health=[True], loaded=[["/models/base"]]),
        events,
        on_disk={"org/base", "org/head"},
    )
    await res.start(lambda s, d, p: None)
    assert events == [("spawn", "")]


async def test_running_engines_are_adopted(tmp_path: Path) -> None:
    events: list[Event] = []
    runner = FakeRunner()
    res = resource(tmp_path, runner, FakeProber(health=[True, True]), events, on_disk=set())
    assert not res.process.pidfile.exists()
    assert await res.running()
    manager = Manager([res], budget_gb=100, clock=FakeClock())
    await manager.adopt()
    assert [s.state for s in manager.status()] == ["ready"]
    assert runner.spawned == [] and events == []


async def test_a_start_failure_reports_the_log_tail(tmp_path: Path) -> None:
    events: list[Event] = []
    runner = FakeRunner()
    res = resource(
        tmp_path, runner, FakeProber(health=[False] * 20), events, on_disk={"org/base", "org/head"}
    )
    spawn = runner.spawn

    def spawn_and_die(argv: list[str], log_path: Path, cwd: Path, env: dict[str, str] | None = None) -> int:
        pid = spawn(argv, log_path, cwd, env)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("loading\nRuntimeError: out of memory\n")
        runner.alive.discard(pid)
        return pid

    runner.spawn = spawn_and_die  # type: ignore[method-assign]
    with pytest.raises(EngineError, match=r"exited during startup[\s\S]*RuntimeError: out of memory"):
        await res.start(lambda s, d, p: None)


def test_every_mlx_vlm_engine_is_a_resource(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    [res] = engine_resources(
        cfg,
        state_dir=tmp_path,
        downloader=cast(Downloader, FakeDownloader([])),
        on_disk=lambda repo, rev: True,
    )
    assert (res.name, res.lifecycle, res.idle_unload_s) == ("main", "on_demand", 600.0)
    assert res.footprint_gb > 17  # the weights and the head, plus the engine's KV


def started_elsewhere(tmp_path: Path, runner: FakeRunner) -> None:
    """Another process's engine: its pid file names a live pid."""
    runner.alive.add(77)
    (tmp_path / "state" / "engines" / "main").mkdir(parents=True)
    (tmp_path / "state" / "engines" / "main" / "pid").write_text("77\n")


async def test_a_start_that_finds_the_engine_already_running_waits_for_it_to_answer(tmp_path: Path) -> None:
    events: list[Event] = []
    runner = FakeRunner()
    started_elsewhere(tmp_path, runner)
    prober = FakeProber(health=[False, False, True])
    res = resource(tmp_path, runner, prober, events, on_disk={"org/base", "org/head"})
    await res.start(lambda s, d, p: None)
    assert runner.spawned == []
    assert prober._health == []  # it probed until the engine answered


async def test_an_engine_already_running_that_never_answers_still_fails_the_start(tmp_path: Path) -> None:
    runner = FakeRunner()
    started_elsewhere(tmp_path, runner)
    res = resource(
        tmp_path,
        runner,
        FakeProber(health=[False] * 5),
        [],
        on_disk={"org/base", "org/head"},
        start_timeout_s=0.0,
    )
    with pytest.raises(EngineError, match="already running"):
        await res.start(lambda s, d, p: None)
