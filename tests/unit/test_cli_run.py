"""`airlie run` calls `start_workflow`; eval still uses the topology orchestrator."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.obs import RunDir
from airlie.orchestrator import RunReport
from airlie.teams import TeamResult
from airlie.workflow.runner import WorkflowResult

runner = CliRunner()


def write_cfg(tmp_path: Path) -> Path:
    for r in ("architect", "summarizer", "judge", "aggregator"):
        (tmp_path / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base"}
            for r in ("architect", "summarizer", "judge", "aggregator")
        },
        "teams": {"k": 1, "orchestrator": "native"},
        "story": {"build_role": "architect"},
        "workflows": {
            "story": {
                "title": "t",
                "inputs": [],
                "nodes": {"a": {"kind": "WORK", "prompt": "x", "outputs": {"r": {"format": "text"}}}},
                "edges": {"a": []},
                "outputs": ["r"],
            }
        },
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    p = tmp_path / "airlie.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_run_defaults_to_story_workflow(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, Path]] = []

    async def fake_start_workflow(name: str, *, cfg, engine, workdir, card=None, **kw):
        calls.append((name, workdir))
        return WorkflowResult(outputs={"r": "ok"}, events=[])

    monkeypatch.setattr("airlie.workflow.trigger.start_workflow", fake_start_workflow)

    class StopEngine:
        async def stop(self) -> None:
            return None

    monkeypatch.setattr(cli, "_http_engine", lambda cfg, eng: StopEngine())

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    r = runner.invoke(cli.app, ["run", "--repo", str(repo), "--config", str(write_cfg(tmp_path))])
    assert r.exit_code == 0, r.output
    assert calls == [("story", repo.resolve())]
    assert "workflow story finished" in r.output


def test_run_unknown_workflow_exits_1(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    r = runner.invoke(
        cli.app,
        ["run", "nope", "--repo", str(repo), "--config", str(write_cfg(tmp_path))],
    )
    assert r.exit_code == 1 and "unknown workflow" in r.output


class StubOrchestrator:
    calls: list[tuple[str, str, str | None]] = []

    def __init__(self, cfg, engine, run_id=None, **kw) -> None:
        self.run_dir = RunDir.create(cfg.observability.state_dir, run_id or "stub")

    async def execute_task(self, goal: str, *, repo: Path, test_cmd: str | None = None) -> RunReport:
        StubOrchestrator.calls.append((goal, str(repo), test_cmd))
        return RunReport(
            "stub",
            self.run_dir,
            goal,
            [TeamResult("team-1", True, "done", "did it", 2, 100, 20, 1234.0)],
            1500.0,
        )


def test_eval_run_materializes_task_and_grades(tmp_path: Path, monkeypatch) -> None:
    StubOrchestrator.calls = []
    monkeypatch.setattr(cli, "_orchestrator", lambda cfg, run_id=None: StubOrchestrator(cfg, None, run_id))
    root = Path(__file__).resolve().parents[2]
    r = runner.invoke(
        cli.app,
        [
            "eval",
            "run",
            "lang_detect",
            "--tasks-dir",
            str(root / "evals" / "tasks"),
            "--config",
            str(write_cfg(tmp_path)),
        ],
    )
    assert r.exit_code == 1, r.output
    goal, repo, test_cmd = StubOrchestrator.calls[0]
    assert "detect_language" in goal and test_cmd == "python -m pytest -q"
    assert Path(repo).joinpath("pyproject.toml").is_file()
    assert "FAIL" in r.output
