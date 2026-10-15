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
    assert "repo_map" in kinds  # design 2026-10-13 §4: every topology injects the repository map
    assert engine.requests[0].messages[1].content.startswith("[Repository map]")
    # each team worked in its own worktree. The fake engine hands replies out in execution order, so which
    # team wrote which file is not fixed; both files exist across the worktrees, none leaked into the repo.
    written = {p.name for i in (1, 2) for p in (report.run_dir.path / f"team-{i}").glob("t*.txt")}
    assert written == {"t1.txt", "t2.txt"}
    assert not (repo / "t1.txt").exists() and not (repo / "t2.txt").exists()
    assert os.path.isdir(report.run_dir.path / "trace")


async def test_orchestrator_aggregates_and_writes_report(tmp_path: Path) -> None:
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
        "teams": {"k": 2, "orchestrator": "native", "max_steps": 5, "stop_on_first_pass": False},
        "tools": {"builtin": ["write_file"]},
        "lts": {"enabled": False},
        "aggregator": {"kind": "llm", "role": "aggregator"},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_dir / "airlie.yaml")
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(
                    tool_calls=[ToolCall("a1", "write_file", json.dumps({"path": "ok.txt", "content": "1"}))]
                ),
                FakeReply(text="team one done"),
                FakeReply(text="team two done"),
            ],
            "aggregator": [FakeReply(text="WINNER: team-1\nTeam one wrote ok.txt and its tests pass.")],
        }
    )
    orch = ParallelAgentTeamOrchestrator(cfg, engine, run_id="r10")
    # test_cmd passes only where ok.txt exists
    report = await orch.execute_task("do it", repo=repo, test_cmd="test -f ok.txt")
    assert report.winner == "team-1"
    assert report.final_answer.startswith("Team one wrote")
    assert report.scores["team-1"] == 1.0 and report.scores["team-2"] == 0.0
    md = (report.run_dir.path / "report.md").read_text()
    assert "team-1" in md and "WINNER" not in md and "Team one wrote" in md
    events = [json.loads(x) for x in report.run_dir.events_path.read_text().splitlines()]
    agg = [e for e in events if e["kind"] == "aggregate"][0]
    assert agg["winner"] == "team-1" and agg["kind"] == "aggregate"
    assert report.tests["team-1"] is True and report.tests["team-2"] is False


async def test_stop_on_first_pass_cancels_other_teams(tmp_path: Path) -> None:
    import asyncio

    repo = git_repo(tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (cfg_dir / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base", "thinking": False}
            for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "teams": {"k": 2, "orchestrator": "native", "max_steps": 50, "stop_on_first_pass": True},
        "tools": {"builtin": ["write_file", "shell"]},
        "lts": {"enabled": False},
        "aggregator": {"kind": "test_score"},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_dir / "airlie.yaml")

    class RoutingFake(FakeEngine):
        """Per-team scripts, selected by the 'You are team-N' line the goal message carries."""

        def __init__(self, per_team: dict[str, list[FakeReply]]) -> None:
            super().__init__({f"coder@{t}": v for t, v in per_team.items()})

        async def generate(self, req, on_token=None):
            team = next(t for t in ("team-1", "team-2") if f"You are {t}." in req.messages[1].content)
            req.role = f"coder@{team}"
            res = await super().generate(req, on_token)
            if team == "team-2":
                await asyncio.sleep(0.05)
            return res

    engine = RoutingFake(
        {
            "team-1": [
                FakeReply(
                    tool_calls=[ToolCall("a1", "write_file", json.dumps({"path": "ok.txt", "content": "1"}))]
                ),
                FakeReply(text="team one done"),
            ],
            "team-2": [
                FakeReply(tool_calls=[ToolCall(f"b{i}", "shell", json.dumps({"cmd": f"echo {i}"}))])
                for i in range(40)
            ],
        }
    )
    orch = ParallelAgentTeamOrchestrator(cfg, engine, run_id="r11")
    report = await asyncio.wait_for(
        orch.execute_task("do it", repo=repo, test_cmd="test -f ok.txt"), timeout=20
    )
    by = {t.team_id: t for t in report.teams}
    assert by["team-1"].ok and report.tests["team-1"] is True
    assert by["team-2"].stop_reason == "cancelled"
    assert report.winner == "team-1"
    events = [json.loads(x) for x in report.run_dir.events_path.read_text().splitlines()]
    assert any(e["kind"] == "early_stop" and e["winner"] == "team-1" for e in events)


async def test_a_single_agent_cut_off_at_max_tokens_is_reported_as_cut_off_not_done(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (cfg_dir / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base", "thinking": False}
            for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "teams": {"k": 1, "orchestrator": "native", "max_steps": 5},
        "tools": {"builtin": ["write_file"]},
        "lts": {"enabled": False},
        "aggregator": {"kind": "test_score"},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_dir / "airlie.yaml")
    engine = FakeEngine({"coder": [FakeReply(reasoning="Thinking, still. ", finish_reason="length")]})
    report = await ParallelAgentTeamOrchestrator(cfg, engine, run_id="r12").execute_task("do it", repo=repo)
    [team] = report.teams
    assert (team.ok, team.stop_reason) == (False, "cut_off") and not report.any_ok
    assert "the reply was cut off at max_tokens" in report.final_answer  # what the person reads
    md = (report.run_dir.path / "report.md").read_text()
    assert "| team-1 | no | cut_off | 0 |" in md
    done = [
        e
        for e in map(json.loads, report.run_dir.events_path.read_text().splitlines())
        if e["kind"] == "team_done"
    ]
    assert (done[0]["ok"], done[0]["stop_reason"]) == (False, "cut_off")


# ---- fork-when-stuck worktree helpers ----


def test_commit_wip_includes_untracked_files_and_children_start_from_it(tmp_path: Path) -> None:
    from airlie.teams.worktree import commit_wip, diff_last_commit

    repo = git_repo(tmp_path)
    trunk = add_worktree(repo, tmp_path / "runs" / "r1" / "trunk", branch="airlie/r1/trunk")
    (trunk / "new_module.py").write_text("x = 1\n")
    (trunk / "README.md").write_text("changed\n")
    sha = commit_wip(trunk, "airlie fork point 1")
    assert len(sha) == 40
    child = add_worktree(repo, tmp_path / "runs" / "r1" / "r1-1", branch="airlie/r1/r1-1", start_point=sha)
    assert (child / "new_module.py").read_text() == "x = 1\n"
    assert (child / "README.md").read_text() == "changed\n"
    diff = diff_last_commit(trunk, max_chars=4000)
    assert "+x = 1" in diff and "+changed" in diff


def test_commit_wip_with_nothing_to_commit_still_returns_a_sha(tmp_path: Path) -> None:
    from airlie.teams.worktree import commit_wip

    repo = git_repo(tmp_path)
    trunk = add_worktree(repo, tmp_path / "runs" / "r2" / "trunk", branch="airlie/r2/trunk")
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=trunk, capture_output=True, text=True
    ).stdout.strip()
    sha = commit_wip(trunk, "empty fork point")
    assert sha != before and len(sha) == 40
