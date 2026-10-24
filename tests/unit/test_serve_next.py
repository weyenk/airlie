"""The built Vue app: `GET /next/*` serves `src/airlie/serve/web_next/` (a Vite build with base `/next/`),
beside today's app at `/` and `/app/*`. The tests use a fake build folder, never a real build."""

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


def test_next_serves_the_page_for_any_app_path(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=True)
    for path in ("/next/", "/next/board", "/next/workflows/AIR-17"):
        r = c.get(path)
        assert r.status_code == 200 and '<div id="app">' in r.text
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-cache"
    assert c.get("/next", follow_redirects=False).headers["location"] == "/next/"


def test_next_serves_built_files_with_a_long_cache(tmp_path: Path) -> None:
    r = client_with(tmp_path, built=True).get("/next/assets/app-1a2b.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "javascript" in r.headers["content-type"]


def test_next_never_serves_outside_its_folder(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("outside the build")
    c = client_with(tmp_path, built=True)
    absolute = f"/next/{tmp_path / 'secret.txt'}"  # "/next//private/.../secret.txt": an absolute path
    for path in (
        "/next/..%2f..%2fpyproject.toml",
        "/next/..%2fsecret.txt",
        "/next/%2e%2e/secret.txt",
        absolute,
    ):
        r = c.get(path)
        assert "outside the build" not in r.text and "pyproject" not in r.text


def test_next_answers_a_path_that_is_not_a_path_with_the_page(tmp_path: Path) -> None:
    r = client_with(tmp_path, built=True).get("/next/%00")
    assert r.status_code == 200 and '<div id="app">' in r.text


def test_next_answers_a_path_too_long_for_the_filesystem_with_the_page(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=True)
    segment = "a" * 300  # past NAME_MAX; the second is a whole path past PATH_MAX
    for path in (f"/next/{segment}", f"/next/assets/{segment}", "/next/" + "/".join(["a" * 200] * 30)):
        r = c.get(path)
        assert r.status_code == 200 and '<div id="app">' in r.text
        assert r.headers["cache-control"] == "no-cache"


def test_an_unbuilt_app_says_how_to_build_it_and_today_app_still_works(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=False)
    for path in ("/next/", "/next/board"):
        r = c.get(path)
        assert r.status_code == 503
        assert r.json() == {"error": "the new web app is not built: run task web:build"}
    assert c.get("/").status_code == 200
