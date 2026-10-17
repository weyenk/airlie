"""The lists the web app reads: GET /v1/airlie/jobs, /runs, /stories and /repos."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi.testclient import TestClient

from airlie.config import load_config
from airlie.serve.app import create_app
from airlie.serve.jobs import Jobs
from airlie.serve.listing import recent_runs, repos, stories
from tests.unit.test_serve import FakeUpstream
from tests.unit.test_serve import cfg as base_cfg


def cfg(tmp_path: Path, **story: Any):  # type: ignore[no-untyped-def]
    base_cfg(tmp_path)  # writes airlie.yaml and the role prompts
    data = yaml.safe_load((tmp_path / "airlie.yaml").read_text())
    data["story"] = {"repos_root": str(tmp_path / "git"), **story}
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def write_run(state: Path, run_id: str, lines: list[str] | None) -> None:
    d = state / "runs" / run_id
    d.mkdir(parents=True)
    if lines is not None:
        (d / "events.jsonl").write_text("".join(lines))


def event(kind: str, ts: float, **fields: Any) -> str:
    return json.dumps({"ts": ts, "run_id": "r", "kind": kind, **fields}) + "\n"


def test_recent_runs_reads_each_log_and_tolerates_a_torn_or_missing_one(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_run(
        state,
        "20261017-010000-aaaa",
        [
            event("run_start", 1.0, goal="fix the build", repo="/r", orchestrator="studio"),
            event("run_done", 9.0, ok=True, wall_ms=8000.0),
        ],
    )
    torn = event("run_start", 2.0, goal="add docs", repo="/r", mode="plan")
    write_run(
        state, "20261017-020000-bbbb", [torn, '{"kind": "step", "tool": "read_fi']
    )  # still being written
    write_run(state, "20261017-030000-cccc", None)  # no events.jsonl at all
    (state / "runs" / "not-a-run.txt").write_text("x")
    out = recent_runs(state)
    assert [r["id"] for r in out] == ["20261017-030000-cccc", "20261017-020000-bbbb", "20261017-010000-aaaa"]
    assert out[2] == {
        "id": "20261017-010000-aaaa",
        "goal": "fix the build",
        "repo": "/r",
        "kind": "run",
        "started": 1.0,
        "ok": True,
        "wall_ms": 8000.0,
    }
    assert out[1]["goal"] == "add docs" and out[1]["kind"] == "plan" and out[1]["ok"] is None
    assert out[0] == {
        "id": "20261017-030000-cccc",
        "goal": None,
        "repo": None,
        "kind": "run",
        "started": None,
        "ok": None,
        "wall_ms": None,
    }
    assert recent_runs(state, limit=1) == out[:1]
    assert recent_runs(tmp_path / "nowhere") == []


def test_stories_come_from_each_state_json(tmp_path: Path) -> None:
    state = tmp_path / "state"
    d = state / "stories" / "HDS-5"
    d.mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps(
            {
                "card": {"key": "HDS-5", "title": "Web search tools"},
                "repo": "/Users/n/git/airlie",
                "stage": "built",
                "blocked": "layer 01 failed its test gate",
            }
        )
    )
    (state / "stories" / "HDS-9").mkdir()  # started, no state yet
    (state / "stories" / "HDS-bad").mkdir()
    (state / "stories" / "HDS-bad" / "state.json").write_text("{not json")
    out = stories(state)
    assert out == [
        {
            "key": "HDS-5",
            "title": "Web search tools",
            "repo": "/Users/n/git/airlie",
            "stage": "built",
            "blocked": "layer 01 failed its test gate",
        },
        {"key": "HDS-9", "title": None, "repo": None, "stage": None, "blocked": None},
        {"key": "HDS-bad", "title": None, "repo": None, "stage": None, "blocked": "state.json is damaged"},
    ]
    assert stories(tmp_path / "nowhere") == []


def test_repos_are_story_repo_and_the_git_checkouts_under_repos_root(tmp_path: Path) -> None:
    root = tmp_path / "git"
    (root / "plain").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root / "airlie")], check=True)
    subprocess.run(["git", "init", "-q", str(root / "OpenHand")], check=True)
    (root / "a-file").write_text("x")
    assert repos(cfg(tmp_path)) == [str(root / "airlie"), str(root / "OpenHand")]  # by name, any case
    mine = tmp_path / "mine"
    subprocess.run(["git", "init", "-q", str(mine)], check=True)
    assert repos(cfg(tmp_path, repo=str(mine))) == [str(mine), str(root / "airlie"), str(root / "OpenHand")]
    assert repos(cfg(tmp_path, repo=str(root / "airlie"))) == [str(root / "airlie"), str(root / "OpenHand")]
    assert repos(cfg(tmp_path, repos_root=str(tmp_path / "nowhere"))) == []


async def test_jobs_list_includes_failed_state_and_error(tmp_path: Path) -> None:
    async def boom() -> dict[str, Any]:
        raise RuntimeError("engine fell over")

    async def hang() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    jobs = Jobs()
    assert jobs.list() == []
    first = jobs.start("plan", "p1", tmp_path / "e1", boom())
    first.started -= 10
    jobs.start("run", "r1", tmp_path / "e2", hang())
    await jobs.status("p1", wait=5)
    out = jobs.list()
    assert [j["id"] for j in out] == ["r1", "p1"]  # newest first
    assert out[0] == {"id": "r1", "kind": "run", "state": "running", "started": out[0]["started"]}
    assert out[1]["state"] == "failed" and out[1]["error"] == "RuntimeError: engine fell over"
    jobs.cancel("r1")


def test_the_listing_routes_answer_with_the_key(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_run(state, "20261017-010000-aaaa", [event("run_start", 1.0, goal="g", repo="/r")])
    (state / "stories" / "HDS-1").mkdir(parents=True)
    (state / "stories" / "HDS-1" / "state.json").write_text(
        json.dumps({"card": {"key": "HDS-1"}, "stage": "new"})
    )
    subprocess.run(["git", "init", "-q", str(tmp_path / "git" / "airlie")], check=True)
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    with TestClient(app) as c:
        for path in ("jobs", "runs", "stories", "repos"):
            assert c.get(f"/v1/airlie/{path}").status_code == 401
        auth = {"Authorization": "Bearer k"}
        assert c.get("/v1/airlie/jobs", headers=auth).json() == {"jobs": []}
        runs = c.get("/v1/airlie/runs", headers=auth).json()["runs"]
        assert [r["id"] for r in runs] == ["20261017-010000-aaaa"] and runs[0]["goal"] == "g"
        assert c.get("/v1/airlie/stories", headers=auth).json()["stories"][0]["key"] == "HDS-1"
        assert c.get("/v1/airlie/repos", headers=auth).json() == {"repos": [str(tmp_path / "git" / "airlie")]}
