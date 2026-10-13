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


def test_run_topology_option_sets_config(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []

    def fake(cfg, run_id=None):  # type: ignore[no-untyped-def]
        seen.append(cfg.topology.kind)
        return StubOrchestrator(cfg, None, run_id)

    monkeypatch.setattr(cli, "_orchestrator", fake)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    r = runner.invoke(
        cli.app,
        ["run", "g", "--repo", str(repo), "--topology", "fork", "--config", str(write_cfg(tmp_path))],
    )
    assert r.exit_code == 0, r.output
    assert seen == ["fork"]
    r = runner.invoke(
        cli.app, ["run", "g", "--repo", str(repo), "--topology", "ring", "--config", str(write_cfg(tmp_path))]
    )
    assert r.exit_code == 2 and "unknown topology" in r.output


def test_run_with_topology_single_selects_the_profile(tmp_path: Path, monkeypatch) -> None:
    seen: list = []

    def fake_orch(cfg, run_id=None):  # type: ignore[no-untyped-def]
        seen.append(cfg)
        return StubOrchestrator(cfg, None, run_id)

    monkeypatch.setattr(cli, "_orchestrator", fake_orch)
    p = write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text())
    data["topology"] = {"kind": "teams", "profiles": {"single": {"teams": {"max_steps": 7}}}}
    p.write_text(yaml.safe_dump(data))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    r = runner.invoke(cli.app, ["run", "go", "--repo", str(repo), "--topology", "single", "-c", str(p)])
    assert r.exit_code == 0, r.output
    assert seen[0].topology.kind == "single" and seen[0].teams.k == 1 and seen[0].teams.max_steps == 7


def test_run_with_unknown_topology_exits_2(tmp_path: Path) -> None:
    p = write_cfg(tmp_path)
    r = runner.invoke(cli.app, ["run", "go", "--repo", str(tmp_path), "--topology", "studio", "-c", str(p)])
    assert r.exit_code == 2 and "single, teams, fork" in r.output
