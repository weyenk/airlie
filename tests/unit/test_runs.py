"""Worktrees, run dirs, event/trace sinks, eval task loader, and the orchestrator over a fake engine."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

from airlie.config import load_config
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.evals import load_task, materialize
from airlie.obs import EventSink, RunDir
from airlie.orchestrator import ParallelAgentTeamOrchestrator
from airlie.teams.worktree import add_worktree, remove_worktree


def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def test_worktree_add_and_remove(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    wt = add_worktree(repo, tmp_path / "runs" / "r1" / "team-1", branch="airlie/r1/team-1")
    assert (wt / "README.md").read_text() == "hi\n"
    assert (
        subprocess.run(
            ["git", "branch", "--show-current"], cwd=wt, capture_output=True, text=True
        ).stdout.strip()
        == "airlie/r1/team-1"
    )
    remove_worktree(repo, wt)
    assert not wt.exists()


def test_rundir_and_event_sink(tmp_path: Path) -> None:
    rd = RunDir.create(tmp_path / "state", run_id="r1")
    sink = EventSink(rd.events_path)
    sink.emit("step", team_id="t1", step=1, ok=True)
    sink.emit("tool", team_id="t1", name="shell")
    lines = [json.loads(x) for x in rd.events_path.read_text().splitlines()]
    assert [x["kind"] for x in lines] == ["step", "tool"]
    assert lines[0]["team_id"] == "t1" and "ts" in lines[0] and lines[0]["run_id"] == "r1"
    rd.write_trace("team-1/step-1.json", {"prompt": "p"})
    assert (rd.path / "trace" / "team-1" / "step-1.json").is_file()


def test_load_and_materialize_eval_task(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    task = load_task(root / "evals" / "tasks" / "lang_detect.yaml")
    assert task.id == "lang_detect" and task.run_cmd.startswith("python")
    repo = materialize(task, tmp_path / "work")
    assert (repo / "pyproject.toml").is_file() and (repo / ".git").is_dir()
    assert not any(
        p.name == "hidden_tests" for p in repo.iterdir()
    )  # hidden tests are not in the agent's repo
    graded = task.grade(repo)
    assert graded.passed is False and "langdetect" in graded.output  # nothing implemented yet → import error


async def test_orchestrator_runs_k_teams_in_worktrees_and_reports(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (cfg_dir / f"{r}.md").write_text(f"you are {r}")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base", "thinking": False}
            for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "teams": {"k": 2, "orchestrator": "native", "max_steps": 5},
        "tools": {"builtin": ["write_file", "run_tests"]},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_dir / "airlie.yaml")
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(
                    tool_calls=[
                        ToolCall("a1", "write_file", json.dumps({"path": "t1.txt", "content": "one"}))
                    ]
                ),
                FakeReply(
                    tool_calls=[
                        ToolCall("b1", "write_file", json.dumps({"path": "t2.txt", "content": "two"}))
                    ]
                ),
                FakeReply(text="team one done"),
                FakeReply(text="team two done"),
            ]
        }
    )
    orch = ParallelAgentTeamOrchestrator(cfg, engine, run_id="r9")
    report = await orch.execute_task("do the thing", repo=repo, test_cmd="true")
    assert sorted(r.team_id for r in report.teams) == ["team-1", "team-2"]
    assert all(r.ok for r in report.teams)
    assert report.run_dir.path.name == "r9"
    events = [json.loads(x) for x in report.run_dir.events_path.read_text().splitlines()]
    kinds = [e["kind"] for e in events]
    assert kinds.count("step") == 2 and kinds.count("team_done") == 2 and "run_done" in kinds
    # each team worked in its own worktree. The fake engine hands replies out in execution order, so which
    # team wrote which file is not fixed; both files exist across the worktrees, none leaked into the repo.
    written = {p.name for i in (1, 2) for p in (report.run_dir.path / f"team-{i}").glob("t*.txt")}
    assert written == {"t1.txt", "t2.txt"}
    assert not (repo / "t1.txt").exists() and not (repo / "t2.txt").exists()
    assert os.path.isdir(report.run_dir.path / "trace")
