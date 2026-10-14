"""Serve jobs: plan → edit → run the edited plan in the background, follow it, cancel it."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from airlie.engine import FakeEngine, FakeReply
from airlie.obs import EventSink
from airlie.router import ChiefRouter
from airlie.serve.app import create_app
from airlie.serve.jobs import Jobs, read_events
from airlie.topology.studio import StudioOrchestrator
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_serve import FakeUpstream
from tests.unit.test_studio_orchestrator import APPROVE, DONE, PLAN, RUN, WRITE_PASS, cfg_for


def finish(client: TestClient, job: str) -> dict[str, Any]:
    """Follow a job to its end with long polls; the last status, with every event."""
    body = client.get(f"/v1/airlie/jobs/{job}", params={"wait": 10}).json()
    events = list(body["events"])
    while body["state"] == "running":
        body = client.get(f"/v1/airlie/jobs/{job}", params={"since": body["next"], "wait": 10}).json()
        events += body["events"]
    return {**body, "events": events}


def app_for(tmp_path: Path, engine: FakeEngine, **kw: Any) -> Any:
    def factory(cfg, k, topology=None):  # type: ignore[no-untyped-def]
        assert topology == "studio"
        return StudioOrchestrator(cfg, engine, router=ChiefRouter())

    return create_app(
        cfg_for(tmp_path), transport=httpx.MockTransport(FakeUpstream()), orchestrator_factory=factory, **kw
    )


def test_a_plan_job_then_the_edited_plan_runs_as_a_job(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"chief": [PLAN], "coder": [WRITE_PASS, RUN, DONE], "reviewer": [APPROVE]})
    with TestClient(app_for(tmp_path, engine)) as client:
        r = client.post("/v1/airlie/plans", json={"goal": "fix the build", "repo": str(repo)})
        assert r.status_code == 202 and r.json()["kind"] == "plan"
        planned = finish(client, r.json()["job_id"])
        assert planned["state"] == "done" and planned["result"]["type"] == "plan"
        assert planned["result"]["run_id"] == r.json()["job_id"] and not planned["result"]["failed"]
        plan = planned["result"]["plan"]
        plan["subtasks"] = plan["subtasks"][:1]  # the person drops the docs subtask
        body = {"goal": "fix the build", "repo": str(repo), "plan": plan, "test_cmd": "./check.sh"}
        r = client.post("/v1/airlie/runs", json={**body, "base": "HEAD", "synthesise": False})
        assert r.status_code == 202 and r.json()["kind"] == "run"  # a base that is a commit is accepted
        done = finish(client, r.json()["job_id"])
        assert client.delete(f"/v1/airlie/jobs/{r.json()['job_id']}").json()["cancelled"] is False  # it ended
    assert done["state"] == "done" and done["result"]["tests"] == {"studio": True}
    assert done["result"]["branch"].startswith("airlie/") and done["result"]["ok"] is True
    assert Path(done["result"]["report"]).is_file()
    routes = [e for e in done["events"] if e["kind"] == "route"]
    assert [(e["chosen"], e["decision"]) for e in routes] == [("coder", "pinned")]
    assert [q.role for q in engine.requests].count("chief") == 1  # the plan call only: no synthesis


def test_a_plan_job_whose_chief_answers_or_fails_says_so_in_its_result(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    body = {"goal": "what is 6 x 7?", "repo": str(repo)}
    answer = FakeReply(text=json.dumps({"type": "answer", "answer": "it is 42"}))
    with TestClient(app_for(tmp_path, FakeEngine({"chief": [answer]}))) as client:
        job = client.post("/v1/airlie/plans", json=body).json()["job_id"]
        done = finish(client, job)
    assert done["state"] == "done"
    assert done["result"] == {"run_id": job, "failed": False, "type": "answer", "answer": "it is 42"}
    with TestClient(app_for(tmp_path, FakeEngine({}))) as client:  # no chief script: the engine errors
        job = client.post("/v1/airlie/plans", json=body).json()["job_id"]
        done = finish(client, job)
    assert done["state"] == "done" and done["result"]["failed"] is True  # a finished job, a failed plan
    assert done["result"]["type"] == "answer" and "engine error" in done["result"]["answer"]


def test_a_plan_or_base_that_cannot_run_is_a_400_and_unknown_jobs_are_404(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    one = {"subtasks": [{"id": "b", "role": "coder", "brief": "write PASS"}]}
    runs = tmp_path / "state" / "runs"

    def left_behind() -> list[Path]:  # the orchestrator makes a run directory when it is built
        return list(runs.iterdir()) if runs.exists() else []

    with TestClient(app_for(tmp_path, FakeEngine({}))) as client:
        bad = {"subtasks": [{"id": "a", "role": "chief", "brief": "x"}]}
        r = client.post("/v1/airlie/runs", json={"goal": "g", "repo": str(repo), "plan": bad})
        assert r.status_code == 400 and r.json()["detail"].startswith("plan rejected: ")
        assert "role 'chief'" in r.json()["detail"]
        r = client.post("/v1/airlie/runs", json={"goal": "g", "repo": str(repo), "plan": {"subtasks": []}})
        assert r.status_code == 400 and "subtasks" in r.json()["detail"]
        edited = {**one, "assumptions": 5}  # a hand-edited field of the wrong type: a 400, not a 500
        r = client.post("/v1/airlie/runs", json={"goal": "g", "repo": str(repo), "plan": edited})
        assert r.status_code == 400 and r.json()["detail"].startswith("plan rejected: ")
        assert left_behind() == []  # a rejected plan makes no run directory
        for base in ("--orphan", "no-such-branch"):
            body = {"goal": "g", "repo": str(repo), "plan": one, "base": base}
            r = client.post("/v1/airlie/runs", json=body)
            assert r.status_code == 400 and "base" in r.json()["detail"]
        assert left_behind() == []  # nor does a rejected base
        r = client.post("/v1/airlie/plans", json={"goal": "g", "repo": str(tmp_path / "nowhere")})
        assert r.status_code == 400
        body = {"goal": "g", "repo": str(repo), "images": [str(tmp_path / "missing.png")]}
        r = client.post("/v1/airlie/plans", json=body)
        assert r.status_code == 400 and "image not found" in r.json()["detail"]
        assert client.get("/v1/airlie/jobs/ghost").status_code == 404
        assert client.delete("/v1/airlie/jobs/ghost").status_code == 404
    assert left_behind() == []


def test_a_negative_since_is_rejected_instead_of_reading_the_tail(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    with TestClient(app_for(tmp_path, FakeEngine({"chief": [PLAN]}))) as client:
        job = client.post("/v1/airlie/plans", json={"goal": "g", "repo": str(repo)}).json()["job_id"]
        done = finish(client, job)
        assert [e["kind"] for e in done["events"]] == ["run_start", "plan", "run_done"]
        assert client.get(f"/v1/airlie/jobs/{job}", params={"since": -1}).status_code == 422
        tail = client.get(f"/v1/airlie/jobs/{job}", params={"since": 2}).json()  # `since` counts events seen
    assert [e["kind"] for e in tail["events"]] == ["run_done"] and tail["next"] == 3


def test_a_long_poll_is_capped_at_five_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def status(self, job_id, *, since=0, wait=0.0):  # type: ignore[no-untyped-def]
        waits.append(wait)
        return {"job_id": job_id, "kind": "run", "state": "done", "events": [], "next": since}

    monkeypatch.setattr(Jobs, "status", status)
    with TestClient(app_for(tmp_path, FakeEngine({}))) as client:
        assert client.get("/v1/airlie/jobs/j1", params={"wait": 99999}).status_code == 200
        assert client.get("/v1/airlie/jobs/j1", params={"wait": 2.5}).status_code == 200
    assert waits == [300.0, 2.5]


def test_a_running_job_can_be_cancelled(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)

    class Hang(FakeEngine):
        async def generate(self, req, on_token=None):  # type: ignore[no-untyped-def]
            await asyncio.sleep(3600)

    one = {"subtasks": [{"id": "b", "role": "coder", "brief": "write PASS"}]}
    with TestClient(app_for(tmp_path, Hang({}))) as client:
        body = {"goal": "g", "repo": str(repo), "plan": one}
        job = client.post("/v1/airlie/runs", json=body).json()["job_id"]
        assert client.get(f"/v1/airlie/jobs/{job}").json()["state"] == "running"
        assert client.delete(f"/v1/airlie/jobs/{job}").json() == {"job_id": job, "cancelled": True}
        body = client.get(f"/v1/airlie/jobs/{job}", params={"since": 10_000, "wait": 5}).json()
    assert body["state"] == "cancelled"


# ---- the registry itself ----


def test_read_events_follows_the_log_and_stops_at_a_line_still_being_written(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    assert read_events(log, 0) == []  # the run has not written anything yet
    log.write_text('{"n": 1}\n{"n": 2}\n{"n": 3')  # the writer is part-way through the third line
    assert read_events(log, 0) == [{"n": 1}, {"n": 2}]
    assert read_events(log, 1) == [{"n": 2}] and read_events(log, 9) == []
    log.write_text('{"n": 1}\n{"n": 2}\n{"n": 3}\n')
    assert read_events(log, 2) == [{"n": 3}]


async def test_status_holds_a_long_poll_until_an_event_or_the_end_or_the_wait_is_up(tmp_path: Path) -> None:
    sink = EventSink(tmp_path / "events.jsonl", "j1")
    release = asyncio.Event()

    async def work() -> dict[str, Any]:
        await asyncio.sleep(0.1)
        sink.emit("a")
        await release.wait()
        return {"done": True}

    jobs = Jobs()
    jobs.start("run", "j1", sink.path, work())
    out = await jobs.status("j1", wait=5)  # held until the event
    assert out is not None and [e["kind"] for e in out["events"]] == ["a"] and out["state"] == "running"
    assert out["next"] == 1
    t0 = time.monotonic()
    out = await jobs.status("j1", since=1, wait=0.3)  # nothing new: held until the wait is up
    assert out is not None and out["events"] == [] and out["next"] == 1 and out["state"] == "running"
    assert time.monotonic() - t0 >= 0.25
    release.set()
    out = await jobs.status("j1", since=1, wait=5)  # held until the job ends
    assert out is not None and out["state"] == "done" and out["result"] == {"done": True}


async def test_a_job_that_raises_ends_failed_with_the_error(tmp_path: Path) -> None:
    async def boom() -> dict[str, Any]:
        raise RuntimeError("engine fell over")

    jobs = Jobs()
    jobs.start("run", "j1", tmp_path / "events.jsonl", boom())
    out = await jobs.status("j1", wait=5)
    assert out is not None and out["state"] == "failed" and out["error"] == "RuntimeError: engine fell over"
    assert "result" not in out and await jobs.status("ghost") is None


async def test_a_running_job_id_cannot_start_twice_but_an_ended_one_can(tmp_path: Path) -> None:
    async def hang() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    async def quick() -> dict[str, Any]:
        return {"ok": True}

    jobs = Jobs()
    jobs.start("run", "j1", tmp_path / "events.jsonl", hang())
    with pytest.raises(ValueError, match="already running"):
        jobs.start("run", "j1", tmp_path / "events.jsonl", hang())
    assert jobs.cancel("j1") is True and jobs.cancel("ghost") is False
    out = await jobs.status("j1", wait=5)
    assert out is not None and out["state"] == "cancelled" and jobs.cancel("j1") is False  # it has ended
    jobs.start("run", "j1", tmp_path / "events.jsonl", quick())
    out = await jobs.status("j1", wait=5)
    assert out is not None and out["state"] == "done" and out["result"] == {"ok": True}
