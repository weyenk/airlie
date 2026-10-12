"""`airlie run` and `airlie eval run` wire the orchestrator; the engine and team are stubbed."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.obs import RunDir
from airlie.orchestrator import RunReport
from airlie.teams import TeamResult

runner = CliRunner()


def write_cfg(tmp_path: Path) -> Path:
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (tmp_path / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "teams": {"k": 1, "orchestrator": "native"},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    p = tmp_path / "airlie.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


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


def test_run_command_passes_goal_repo_and_k(tmp_path: Path, monkeypatch) -> None:
    StubOrchestrator.calls = []
    monkeypatch.setattr(cli, "_orchestrator", lambda cfg, run_id=None: StubOrchestrator(cfg, None, run_id))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # the command only checks for a git checkout; the stub never touches it
    r = runner.invoke(
        cli.app,
        [
            "run",
            "fix the bug",
            "--repo",
            str(repo),
            "--k",
            "2",
            "--test-cmd",
            "pytest -q",
            "--config",
            str(write_cfg(tmp_path)),
        ],
    )
    assert r.exit_code == 0, r.output
    assert StubOrchestrator.calls == [("fix the bug", str(repo.resolve()), "pytest -q")]
    assert "team-1" in r.output and "did it" in r.output


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
    assert r.exit_code == 1, r.output  # stub team wrote nothing, so hidden tests fail
    goal, repo, test_cmd = StubOrchestrator.calls[0]
    assert "detect_language" in goal and test_cmd == "python -m pytest -q"
    assert Path(repo).joinpath("pyproject.toml").is_file()
    assert "FAIL" in r.output
