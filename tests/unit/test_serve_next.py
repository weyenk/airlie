"""The built Vue app: `GET /` and every path that is not the API, a file or the health check serve
`src/airlie/serve/web_next/` (a Vite build with base `/`); `/next/...` redirects to the same path without the
prefix. The tests use a fake build folder, never a real build."""

from __future__ import annotations

from pathlib import Path

import httpx

from airlie.serve.app import create_app
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream, cfg


def client_with(tmp_path: Path, *, built: bool) -> TestClient:
    next_dir = tmp_path / "web_next"
    if built:
        (next_dir / "assets").mkdir(parents=True)
        (next_dir / "index.html").write_text('<!doctype html><div id="app"></div>')
        (next_dir / "assets" / "app-1a2b.js").write_text("console.log('app')")
    return TestClient(
        create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), next_dir=next_dir)
    )


def test_every_app_path_serves_the_new_app_and_next_redirects(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=True)
    for path in ("/", "/board", "/board/AIR-17", "/workflows/AIR-17", "/join/tok", "/signin"):
        r = c.get(path)
        assert r.status_code == 200 and '<div id="app">' in r.text, path
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-cache"
    r = c.get("/next/workflows?blocked=true", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/workflows?blocked=true"
    for path, to in (("/next", "/"), ("/next/", "/"), ("/next/board/AIR-17", "/board/AIR-17")):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == to, path
    assert c.get("/app/app.js").status_code == 404 and c.get("/health").status_code == 200


def test_next_never_redirects_off_the_site(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=True)
    for path in (
        "/next//evil.com",
        "/next/%2Fevil.com",
        "/next/%2F%2Fevil.com",
        "/next///evil.com/x?a=1",
        "/next/%5Cevil.com",
        "/next/\\evil.com",
        "/next/%2F%5Cevil.com",
    ):
        r = c.get(path, follow_redirects=False)
        location = r.headers["location"]
        assert r.status_code == 308, path
        assert location.startswith("/") and not location.startswith(("//", "/\\")), (path, location)
        assert "evil.com" in location and "\\" not in location, (path, location)
    assert c.get("/next///evil.com/x?a=1", follow_redirects=False).headers["location"] == "/evil.com/x?a=1"


def test_a_missing_file_is_a_404_not_the_page(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=True)
    for path in ("/assets/missing.js", "/favicon.ico", "/robots.txt", "/board/x.map"):
        r = c.get(path)
        assert r.status_code == 404 and '<div id="app">' not in r.text, path
    for path in ("/board/AIR-17", "/workflows"):
        assert '<div id="app">' in c.get(path).text


def test_the_api_keeps_its_guard_and_unknown_api_paths_stay_json_404s(tmp_path: Path) -> None:
    app = create_app(
        cfg(tmp_path),
        transport=httpx.MockTransport(FakeUpstream()),
        api_key="k",
        next_dir=tmp_path / "web_next",
    )
    (tmp_path / "web_next").mkdir()
    (tmp_path / "web_next" / "index.html").write_text('<!doctype html><div id="app"></div>')
    c = TestClient(app)
    assert c.get("/").status_code == 200  # the shell needs no key
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"Authorization": "Bearer k"}).status_code == 200
    r = c.get("/v1/nothing/here", headers={"Authorization": "Bearer k"})
    assert r.status_code == 404 and '<div id="app">' not in r.text


def test_the_page_serves_built_files_with_a_long_cache(tmp_path: Path) -> None:
    r = client_with(tmp_path, built=True).get("/assets/app-1a2b.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "javascript" in r.headers["content-type"]


def test_it_never_serves_outside_its_folder(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("outside the build")
    c = client_with(tmp_path, built=True)
    absolute = f"/{tmp_path / 'secret.txt'}"  # "//private/.../secret.txt": an absolute path
    for path in (
        "/..%2f..%2fpyproject.toml",
        "/..%2fsecret.txt",
        "/%2e%2e/secret.txt",
        "/assets/..%2f..%2fsecret.txt",
        absolute,
    ):
        r = c.get(path)
        assert "outside the build" not in r.text and "pyproject" not in r.text


def test_a_path_that_is_not_a_path_gets_the_page(tmp_path: Path) -> None:
    r = client_with(tmp_path, built=True).get("/%00")
    assert r.status_code == 200 and '<div id="app">' in r.text


def test_a_path_too_long_for_the_filesystem_gets_the_page(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=True)
    segment = "a" * 300  # past NAME_MAX; the last is a whole path past PATH_MAX
    for path in (f"/{segment}", f"/board/{segment}", "/" + "/".join(["a" * 200] * 30)):
        r = c.get(path)
        assert r.status_code == 200 and '<div id="app">' in r.text
        assert r.headers["cache-control"] == "no-cache"


def test_an_unbuilt_app_says_how_to_build_it(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=False)
    for path in ("/", "/board"):
        r = c.get(path)
        assert r.status_code == 503
        assert r.json() == {"error": "the web app is not built: run task web:build"}
    assert c.get("/health").status_code == 200
