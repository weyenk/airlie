"""The flow route following a real plan job over a real git repo to its end: the plan it adds for a finished
plan job, its 404s, and a plan job that answered instead of planning. The engine-free flow shaping is pinned
in tests/unit/test_serve_run_flow.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airlie.engine import FakeEngine, FakeReply
from tests.support.git import repo_with_marker_tests
from tests.support.serve_jobs import app_for, finish
from tests.support.serve_tree import STUDIO_START
from tests.support.serve_tree import write_run as write_log
from tests.support.studio import PLAN
from tests.unit.serve_helpers import TestClient


def write_run(state: Path, run: str, events: list[dict[str, Any]]) -> Path:
    """Run `run`'s event log under `state`; returns `state`."""
    write_log(state, run, events)
    return state


def lanes(flow: dict[str, Any]) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Each stage's id and status, with its jobs' ids and statuses."""
    return [(n["id"], n["status"], [(j["id"], j["status"]) for j in n["jobs"]]) for n in flow["nodes"]]


def test_the_flow_route_adds_the_plan_of_a_finished_plan_job_and_404s_an_unknown_run(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    with TestClient(app_for(tmp_path, FakeEngine({"chief": [PLAN]}))) as client:
        job = client.post("/v1/airlie/plans", json={"goal": "fix the build", "repo": str(repo)}).json()[
            "job_id"
        ]
        planned = finish(client, job)
        assert planned["state"] == "done" and planned["result"]["type"] == "plan"
        r = client.get(f"/v1/airlie/runs/{job}/flow")
        assert r.status_code == 200
        body = r.json()
        assert body["plan"] == planned["result"]["plan"]
        assert [s["id"] for s in body["plan"]["subtasks"]] == ["fix", "note"]
        assert body["job"] == "done" and body["key"] == job and body["kind"] == "run"
        assert body["mode"] == "plan" and lanes(body) == [("plan", "passed", [("chief", "passed")])]
        chief = client.get(f"/v1/airlie/runs/{job}/flow/plan/chief")
        assert chief.status_code == 200 and chief.json()["job"]["id"] == "chief"
        for path in ("nope/flow", "nope/flow/plan/chief", f"{job}/flow/nope/chief", f"{job}/flow/plan/nope"):
            assert client.get(f"/v1/airlie/runs/{path}").status_code == 404, path
        state = tmp_path / "state"
        write_run(state, "20261024-000000-dead", STUDIO_START)  # a run this server did not start
        other = client.get("/v1/airlie/runs/20261024-000000-dead/flow").json()
        assert other["job"] is None and other["plan"] is None and other["stage"] == "running"


def test_a_plan_job_that_answered_instead_has_no_plan_to_approve(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    answer = FakeReply(text=json.dumps({"type": "answer", "answer": "it is 42"}))
    with TestClient(app_for(tmp_path, FakeEngine({"chief": [answer]}))) as client:
        job = client.post("/v1/airlie/plans", json={"goal": "6 x 7?", "repo": str(repo)}).json()["job_id"]
        assert finish(client, job)["result"]["type"] == "answer"
        body = client.get(f"/v1/airlie/runs/{job}/flow").json()
    assert body["job"] == "done" and body["plan"] is None
