"""The flow route through its TestClient when a plan job fails before its run_done: the flow is done, nothing
runs, and the job detail agrees. The engine-free flow shaping is pinned in
tests/unit/test_serve_run_flow.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from airlie.obs.run_dir import RunDir
from airlie.serve.app import create_app
from tests.support.serve import FakeUpstream
from tests.support.serve_jobs import finish
from tests.support.serve_tree import STUDIO_START
from tests.support.studio import cfg_for
from tests.unit.serve_helpers import TestClient


def lanes(flow: dict[str, Any]) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Each stage's id and status, with its jobs' ids and statuses."""
    return [(n["id"], n["status"], [(j["id"], j["status"]) for j in n["jobs"]]) for n in flow["nodes"]]


def running_anything(flow: dict[str, Any]) -> bool:
    """Whether any stage or job of the flow says it is running."""
    return any(
        n["status"] == "running" or any(j["status"] == "running" for j in n["jobs"]) for n in flow["nodes"]
    )


def test_a_run_whose_job_failed_before_its_run_done_is_done(tmp_path: Path) -> None:
    config = cfg_for(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    class Breaks:
        def __init__(self) -> None:
            self.run_dir = RunDir.create(config.observability.state_dir, "20261024-000000-beef")

        async def plan(self, goal: str, **_: Any) -> Any:
            self.run_dir.events_path.write_text(json.dumps(STUDIO_START[0]) + "\n")  # its run_start, no more
            raise RuntimeError("engine fell over")

    app = create_app(
        config, transport=httpx.MockTransport(FakeUpstream()), orchestrator_factory=lambda *a, **k: Breaks()
    )
    with TestClient(app) as client:
        job = client.post("/v1/airlie/plans", json={"goal": "g", "repo": str(repo)}).json()["job_id"]
        assert finish(client, job)["state"] == "failed"
        body = client.get(f"/v1/airlie/runs/{job}/flow").json()
        chief = client.get(f"/v1/airlie/runs/{job}/flow/plan/chief").json()
    assert body["job"] == "failed" and body["stage"] == "done" and body["plan"] is None
    assert body["topology"] == "studio" and not running_anything(body)
    assert lanes(body) == [("plan", "failed", [("chief", "failed")]), ("specialists", "pending", [])]
    assert chief["job"]["status"] == "failed"  # the job detail agrees with the flow
