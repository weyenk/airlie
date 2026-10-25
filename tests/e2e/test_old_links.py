"""Links made for the old app (`/#/board/AIR-17`) and its `/next` home land on the clean path in Chromium.
Each check leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import Evidence, Server

pytestmark = pytest.mark.e2e


def test_an_old_hash_link_lands_on_its_clean_path(app_page: Page, server: Server, evidence: Evidence) -> None:
    app_page.goto(f"{server.url}/#/board/AIR-3")
    evidence.expect(app_page, "the hash became a path").to_have_url(re.compile(r"/board/AIR-3$"))
    evidence.expect(app_page.locator("[data-item=AIR-3]"), "the board drew").to_be_visible()


def test_an_old_invite_link_lands_on_join(page: Page, server: Server, evidence: Evidence) -> None:
    page.goto(f"{server.url}/#/join/not-a-live-token")
    evidence.expect(page, "the invite hash became a path").to_have_url(re.compile(r"/join/not-a-live-token$"))


def test_the_next_prefix_redirects_to_the_same_path(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    app_page.goto(f"{server.url}/next/workflows?blocked=true")
    evidence.expect(app_page, "the prefix is gone, the query kept").to_have_url(
        re.compile(r"/workflows\?blocked=true$")
    )
