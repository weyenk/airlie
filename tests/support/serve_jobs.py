"""Shared helpers extracted from tests/unit/test_serve_jobs.py for tests.support.serve_jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from airlie.engine import FakeEngine
from airlie.router import ChiefRouter
from airlie.serve.app import create_app
from airlie.topology.studio import StudioOrchestrator
from tests.support.serve import FakeUpstream
from tests.support.studio import cfg_for
from tests.unit.serve_helpers import TestClient


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
