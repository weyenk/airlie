"""Serve gates the catalog and completions on the airlie-wide model allowlist."""

from __future__ import annotations

from pathlib import Path

import httpx

from airlie.models.allowed import allowed_ids
from airlie.models.allowlist import ModelAllowlist
from airlie.serve.app import create_app
from tests.support.serve import FakeUpstream, cfg
from tests.unit.serve_helpers import TestClient


def _switch_off(tmp_path: Path, *ids: str) -> None:
    allowlist = ModelAllowlist(tmp_path / "state" / "models.sqlite")
    for i in ids:
        allowlist.set_allowed(i, False)
    allowlist.close()


def test_allowed_ids_drops_switched_off_models_and_keeps_order(tmp_path: Path) -> None:
    allowlist = ModelAllowlist(tmp_path / "models.sqlite")
    allowlist.set_allowed("b", False)
    assert allowed_ids(allowlist, ["c", "b", "a"]) == ["c", "a"]
    allowlist.close()


def test_disallowed_raw_catalog_model_is_not_served(tmp_path: Path) -> None:
    _switch_off(tmp_path, "small")
    up = FakeUpstream()
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(up))
    with TestClient(app) as c:
        ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
        assert "small" not in ids and "base" in ids
        assert {"architect", "summarizer", "judge", "aggregator"} <= set(ids)  # roles stay listed
        r = c.post("/v1/chat/completions", json={"model": "small", "messages": []})
        assert r.status_code == 400 and "not allowed in airlie" in r.json()["detail"]
        assert "small" in r.json()["detail"]
    assert up.requests == []


def test_a_role_whose_model_is_disallowed_is_listed_but_refused(tmp_path: Path) -> None:
    _switch_off(tmp_path, "base")
    up = FakeUpstream()
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(up))
    with TestClient(app) as c:
        assert "architect" in [m["id"] for m in c.get("/v1/models").json()["data"]]
        r = c.post("/v1/chat/completions", json={"model": "architect", "messages": []})
        assert r.status_code == 400 and "model 'base' is not allowed in airlie" in r.json()["detail"]
        ok = c.post(
            "/v1/chat/completions",
            json={"model": "summarizer", "messages": [{"role": "user", "content": "x"}]},
        )
        assert ok.status_code == 200
    assert len(up.requests) == 1


def test_a_model_switched_off_by_its_served_name_is_refused(tmp_path: Path) -> None:
    _switch_off(tmp_path, "tiny")  # `small` is served upstream as `tiny`
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    with TestClient(app) as c:
        assert "small" not in [m["id"] for m in c.get("/v1/models").json()["data"]]
        r = c.post("/v1/chat/completions", json={"model": "small", "messages": []})
        assert r.status_code == 400 and "not allowed in airlie" in r.json()["detail"]
