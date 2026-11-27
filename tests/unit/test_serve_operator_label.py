"""The sign-in screen reads `auth.operator_label` from `GET /v1/auth/operator`. It is a public read — a person
on `/signin` or `/join/<token>` is nobody yet, so `/auth/me` cannot carry the copy those pages show — and a
default install answers the shipped role phrase, which is what the web app's fallback must agree with."""

from __future__ import annotations

from pathlib import Path

import httpx
import yaml

from airlie.config import load_config
from airlie.serve.app import create_app
from tests.support.serve import FakeUpstream, cfg
from tests.unit.serve_helpers import TestClient


def client(tmp_path: Path, auth: dict[str, object] | None = None) -> TestClient:
    """serve with `auth` merged over the shared fixture config, called from a tailnet address so nothing here
    can be the fresh-install bootstrap admin: the label must not depend on who is asking."""
    # cfg() writes the fixture's airlie.yaml and returns it loaded, so the merge reads what it just wrote.
    loaded = cfg(tmp_path)
    if auth is not None:
        data = yaml.safe_load((tmp_path / "airlie.yaml").read_text())
        data["auth"] = auth
        (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
        loaded = load_config(tmp_path / "airlie.yaml")
    return TestClient(
        create_app(loaded, transport=httpx.MockTransport(FakeUpstream())),
        client=("10.0.0.5", 1),
    )


def test_a_default_install_answers_the_shipped_role_phrase(tmp_path: Path) -> None:
    """The regression guard: an install that never mentions the key answers the phrase the web app falls back
    to, and never one person's name."""
    with client(tmp_path) as c:
        answer = c.get("/v1/auth/operator").json()
        assert answer == {"operator_label": "your administrator"}
        assert "Noah" not in answer["operator_label"]


def test_a_configured_label_reaches_the_browser(tmp_path: Path) -> None:
    with client(tmp_path, {"operator_label": "your workspace admin"}) as c:
        assert c.get("/v1/auth/operator").json() == {"operator_label": "your workspace admin"}


def test_it_is_read_before_anyone_is_anybody(tmp_path: Path) -> None:
    """Public by need, not by taste: signed out, and with no session cookie to trade on."""
    with client(tmp_path, {"operator_label": "Dana"}) as c:
        assert c.get("/v1/auth/me").status_code == 401
        r = c.get("/v1/auth/operator")
        assert r.status_code == 200 and r.json()["operator_label"] == "Dana"
        assert "airlie_session" not in r.headers.get("set-cookie", "")
