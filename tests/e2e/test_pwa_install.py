"""What makes the app installable and keeps it signed in: the manifest the page links, the worker's reach (the
shell and assets, never the API), and the session across a reload. Each check leaves its screenshot in
test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import KEY, Evidence, Server

pytestmark = pytest.mark.e2e


def test_the_manifest_names_the_app_and_opens_it_standalone(
    page: Page, server: Server, evidence: Evidence
) -> None:
    page.goto(server.url + "/signin")
    href = page.locator('link[rel="manifest"]').get_attribute("href")
    assert href == "/manifest.webmanifest"
    response = page.request.get(server.url + href)
    manifest = response.json()
    page.goto(server.url + href)
    evidence.shot("the manifest is served", held=response.ok)
    assert response.ok
    assert manifest["name"] == "airlie"
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])
    for icon in manifest["icons"]:
        served = page.request.get(server.url + icon["src"])
        assert served.ok and served.headers["content-type"].startswith("image/png"), icon["src"]
    evidence.shot("it names the app, starts at /, is standalone and its icons load")


def test_the_worker_serves_the_shell_but_never_the_api(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    app_page.goto(server.url + "/")
    app_page.evaluate("navigator.serviceWorker.ready")
    app_page.reload()
    app_page.get_by_label("Message", exact=True).wait_for()
    evidence.expect(
        app_page.get_by_label("Message", exact=True), "the app is up under its worker"
    ).to_be_visible()
    from_worker = app_page.evaluate(
        """async () => {
            const key = localStorage.getItem('airlie.key');
            const r = await fetch('/v1/airlie/engines', {headers: {Authorization: 'Bearer ' + key}});
            return {status: r.status, body: await r.json()};
        }"""
    )
    evidence.shot("the page's own API call is answered live", held=from_worker["status"] == 200)
    assert from_worker["status"] == 200
    # the worker's cache holds no /v1 answer, so a stale one can never stand in for the Studio's
    cached = app_page.evaluate(
        """async () => {
            const out = [];
            for (const name of await caches.keys()) {
                for (const req of await (await caches.open(name)).keys()) out.push(new URL(req.url).pathname);
            }
            return out;
        }"""
    )
    evidence.shot(
        "the worker's cache holds no API answer", held=not any(p.startswith("/v1/") for p in cached)
    )
    assert cached, "the worker cached the shell"
    assert "/" in cached
    assert not any(p.startswith("/v1/") for p in cached)


def test_the_session_survives_a_reload(page: Page, server: Server, evidence: Evidence) -> None:
    page.goto(f"{server.url}/board")
    page.get_by_text("Sign in with the admin key").click()
    page.get_by_label("Admin key").fill(KEY)
    page.get_by_role("button", name="Sign in with the key").click()
    evidence.expect(page.get_by_role("region", name="Doing"), "signed in, the board shows").to_be_visible()
    page.evaluate("navigator.serviceWorker.ready")
    page.reload()
    evidence.expect(
        page.get_by_role("region", name="Doing"), "after a reload the board is still there"
    ).to_be_visible()
    evidence.expect(page, "no sign-in in between").not_to_have_url(re.compile(r"/signin"))
    page.goto(f"{server.url}/")
    page.get_by_label("Message", exact=True).wait_for()
    evidence.expect(
        page.get_by_label("Message", exact=True), "nor on another page of the app"
    ).to_be_visible()
