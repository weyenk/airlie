"""Bridge platform tests T10–T12: studio kind mapping, serve 404 → failed, claim loop with fake clock."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.config import load_config
from airlie.config.schema import AllowedRepoSpec, BridgeSpec
from airlie.serve.bridge.airlie_client import ServeClient
from airlie.serve.bridge.clock import FakeClock
from airlie.serve.bridge.errors import BridgeError
from airlie.serve.bridge.kinds import StudioKind, map_run_result
from airlie.serve.bridge.payload import assert_data_payload
from airlie.serve.bridge.repos import resolve_repo
from airlie.serve.bridge.runner import run_bridge

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml


def _cfg_with_bridge(bridge: BridgeSpec) -> Any:
    cfg = load_config(ROOT / "airlie.yaml", local_path=NO_LOCAL)
    cfg.bridge = bridge
    return cfg


def bridge_cfg(tmp_path: Path, **over: Any) -> BridgeSpec:
    repo = tmp_path / "proj"
    repo.mkdir()
    return BridgeSpec(
        job_bus_url="https://hub.test/api/v1",
        allowed_repos=[AllowedRepoSpec(key="main", path=repo.resolve(), default=True)],
        test_cmds=["pytest -q"],
        **over,
    )


class FakeHub(httpx.AsyncBaseTransport):
    def __init__(self, *, jobs: list[dict[str, Any]] | None = None) -> None:
        self.claim_calls = 0
        self.results: list[dict[str, Any]] = []
        self._queue = list(jobs or [])

    def enqueue(self, job: dict[str, Any]) -> None:
        self._queue.append(job)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/jobs/claim"):
            self.claim_calls += 1
            if not self._queue:
                return httpx.Response(204)
            job = self._queue.pop(0)
            return httpx.Response(200, json={"job": job})
        if request.method == "POST" and request.url.path.endswith("/result"):
            self.results.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "unexpected hub route"})


class FakeServe(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self._state = "running"
        self._result: dict[str, Any] = {}
        self._job_id = "20261028-test-run"
        self.polls = 0
        self._missing = False

    def set_done(self, result: dict[str, Any]) -> None:
        self._result = result
        self._state = "done"

    def set_missing(self) -> None:
        self._missing = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/airlie/runs":
            self.posts.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": self._job_id, "kind": "run"})
        if request.method == "GET" and request.url.path.startswith("/v1/airlie/jobs/"):
            self.polls += 1
            if self._missing:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(
                200,
                json={"state": self._state, "result": self._result, "next": 0, "events": []},
            )
        return httpx.Response(404)


ENV = {"BRIDGE_WORKER_SECRET": "s" * 40}


def test_credential_env_reads_bridge_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker secrets come from ~/.config/airlie/bridge.env, not the shared ATLASSIAN .env."""
    home = tmp_path / "home"
    airlie_cfg = home / ".config" / "airlie"
    airlie_cfg.mkdir(parents=True)
    dotenv = airlie_cfg / "bridge.env"
    dotenv.write_text("BRIDGE_WORKER_SECRET=" + "b" * 40 + "\n")
    dotenv.chmod(0o600)
    (airlie_cfg / ".env").write_text("BRIDGE_WORKER_SECRET=" + "wrong" * 8 + "\n")
    monkeypatch.setenv("HOME", str(home))
    for key in ("BRIDGE_WORKER_SECRET", "AIRLIE_SERVE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    cfg = _cfg_with_bridge(bridge_cfg(tmp_path))
    from airlie.serve.bridge.session import credential_env, worker_secret

    assert worker_secret(cfg.bridge, credential_env(cfg)) == "b" * 40


# ---- T10: studio mapping, plan=None, report dropped, repo + payload rules ----


def test_t10_studio_posts_plan_none_and_maps_result_without_report(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path)
    serve_t = FakeServe()
    serve_t.set_done(
        {
            "ok": True,
            "final_answer": "done",
            "branch": "airlie/x",
            "tests": {"studio": True},
            "report": "/tmp/report.md",
        }
    )
    client = ServeClient("http://127.0.0.1:8090", transport=serve_t)
    kind = StudioKind(bridge, client, clock=FakeClock())
    out = asyncio.run(kind.run({"goal": "fix it", "repo_key": "main"}))
    assert out.status == "done" and out.result == {
        "ok": True,
        "final_answer": "done",
        "branch": "airlie/x",
        "tests": {"studio": True},
    }
    assert "report" not in (out.result or {})
    assert serve_t.posts == [
        {
            "goal": "fix it",
            "repo": str(bridge.allowed_repos[0].path),
            "plan": None,
            "test_cmd": "pytest -q",
        }
    ]


def test_t10_rejects_control_fields_and_resolves_default_repo(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path)
    with pytest.raises(BridgeError, match="control fields"):
        assert_data_payload({"goal": "g", "repo": "/evil"})
    assert resolve_repo(bridge, None) == bridge.allowed_repos[0].path
    with pytest.raises(BridgeError, match="unknown repo_key"):
        resolve_repo(bridge, "nope")


def test_t10_map_run_answer_type() -> None:
    assert map_run_result({"type": "answer", "answer": "42", "failed": False}) == {
        "ok": True,
        "final_answer": "42",
    }


# ---- T11: serve job 404 → hub failed ----


def test_t11_serve_404_yields_failed_outcome(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path)
    serve_t = FakeServe()
    serve_t.set_missing()
    client = ServeClient("http://127.0.0.1:8090", transport=serve_t)
    out = asyncio.run(StudioKind(bridge, client).run({"goal": "x"}))
    assert out.status == "failed"
    assert out.error_message and "404" in out.error_message


# ---- T12: runner claim loop with fake clock ----


def test_t12_empty_queue_waits_claim_poll_on_fake_clock(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path, claim_poll_s=5.0)
    cfg = _cfg_with_bridge(bridge)
    hub_t = FakeHub()
    clock = FakeClock()

    n = asyncio.run(
        run_bridge(
            cfg,
            env=ENV,
            hub_transport=hub_t,
            serve_transport=FakeServe(),
            clock=clock,
            max_cycles=1,
        )
    )
    assert n == 0
    assert hub_t.claim_calls == 1
    assert clock.sleeps == [5.0]


def test_t12_runner_claims_one_job_and_posts_hub_result(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path)
    cfg = _cfg_with_bridge(bridge)
    jid = str(uuid.uuid4())
    hub_t = FakeHub(
        jobs=[
            {
                "id": jid,
                "kind": "studio",
                "tenant_id": "lhr",
                "payload": {"goal": "ship"},
                "worker_id": "w1",
                "status": "running",
            }
        ]
    )
    serve_t = FakeServe()
    serve_t.set_done({"ok": True, "final_answer": "y", "branch": "b", "tests": {}})

    n = asyncio.run(
        run_bridge(
            cfg,
            env=ENV,
            once=True,
            hub_transport=hub_t,
            serve_transport=serve_t,
        )
    )
    assert n == 1 and hub_t.claim_calls == 1
    assert hub_t.results[0]["status"] == "done"
    assert hub_t.results[0]["result"] == {"ok": True, "final_answer": "y", "branch": "b", "tests": {}}
