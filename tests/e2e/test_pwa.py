"""The app is installable and its service worker controls the page in Chromium.
Each check leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import Evidence, Server

pytestmark = pytest.mark.e2e


def test_the_app_is_installable_and_its_worker_controls_the_page(
    page: Page, server: Server, evidence: Evidence
) -> None:
    page.goto(server.url + "/")
    evidence.expect(page.locator('link[rel="manifest"]'), "the page links its manifest").to_have_attribute(
        "href", "/manifest.webmanifest"
    )
    page.evaluate("navigator.serviceWorker.ready")
    page.reload()
    controlled = page.evaluate("navigator.serviceWorker.controller !== null")
    evidence.shot("the worker controls the reloaded page", held=bool(controlled))
    assert controlled
