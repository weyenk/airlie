"""The web app's static shell: `GET /` and `/app/*` serve `src/airlie/serve/web/` to anyone; `/v1` keeps the
key guard."""

from __future__ import annotations

from pathlib import Path

import httpx

from airlie.serve.app import WEB_DIR, create_app
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream, cfg


def test_root_serves_the_shell(tmp_path: Path) -> None:
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream())))
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>airlie" in r.text
    assert r.headers["cache-control"] == "no-cache"  # revalidated on every load, so a redeploy shows at once
    assert (WEB_DIR / "index.html").is_file()


def test_app_files_are_served_under_app(tmp_path: Path) -> None:
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream())))
    css = client.get("/app/app.css")
    assert css.status_code == 200 and css.headers["cache-control"] == "no-cache"
    r = client.get("/app/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert client.get("/app/missing.js").status_code == 404


def test_static_shell_is_public_but_v1_needs_the_key(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/app/app.css").status_code == 200
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer k"}).status_code == 200


def test_the_sign_in_join_and_people_screens_are_served(tmp_path: Path) -> None:
    """Task 6 of the sign-in plan: the screens exist and are wired; a browser check covers their behaviour."""
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream())))
    signin = client.get("/app/screens/signin.js").text
    assert "Use this device" in signin and "Admin key" in signin and "whoami" in signin
    join = client.get("/app/screens/join.js")
    assert join.status_code == 200 and "/v1/auth/register/options" in join.text
    assert "Create passkey" in join.text
    admin = client.get("/app/screens/admin.js")
    assert admin.status_code == 200 and "/v1/auth/users" in admin.text and "/v1/auth/invites" in admin.text
    app_js = client.get("/app/app.js").text
    assert "#/join/" in app_js.replace("\\/", "/") or "join" in app_js
    assert "screens/join.js" in app_js and "screens/admin.js" in app_js
    api = client.get("/app/api.js").text
    assert 'credentials: "same-origin"' in api and "navigator.credentials" in api
