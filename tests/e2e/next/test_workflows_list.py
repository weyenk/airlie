"""The Vue app's Workflows list in Chromium (/next/workflows): what is running, the stories with their blocked
reasons, the Blocked filter in the address, and a list that follows a story's state on disk. Each check leaves
its screenshot in test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import STORY_KEY, Evidence, Server

pytestmark = pytest.mark.e2e

RUNNING_KEY = "AIR-9"
RUNNING_TITLE = "Context gathering for the coder"
BLOCKED_REASON = "layer 02 (Full screen toggle): its tests still do not test its change"
"""What conftest seeds as STORY_KEY's blocked reason."""


def _write_story(state: Path, key: str, **fields: Any) -> None:
    """A story's state.json as the story tick leaves it, written straight into the file (no request)."""
    story = state / "stories" / key
    story.mkdir(parents=True, exist_ok=True)
    body = {"card": {"key": key, "title": RUNNING_TITLE}, "repo": "weyenk/airlie", "stage": "refine"}
    (story / "state.json").write_text(json.dumps({**body, **fields}))


def _live(page: Page, evidence: Evidence) -> None:
    evidence.expect(page.get_by_text("Live", exact=True), "the feed is live").to_be_visible()


def test_a_running_storys_row_opens_its_workflow_view(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    _write_story(own_server.state, RUNNING_KEY)
    next_page.goto(own_server.url + "/next/workflows")
    row = next_page.locator(f"[data-running={RUNNING_KEY}]")
    evidence.expect(row, "the running story is on the Running card").to_contain_text(RUNNING_TITLE)
    evidence.expect(row.get_by_role("progressbar"), "with a progress bar").to_be_visible()
    row.click()
    evidence.expect(next_page, "the row opens its workflow view").to_have_url(
        re.compile(rf"/next/workflows/{RUNNING_KEY}$")
    )


def test_a_blocked_story_shows_its_reason_and_appears_under_blocked(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    _write_story(own_server.state, RUNNING_KEY)
    next_page.goto(own_server.url + "/next/workflows")
    blocked = next_page.locator(f"[data-story={STORY_KEY}]")
    evidence.expect(blocked, "the blocked story's row shows why").to_contain_text(
        f"Blocked: {BLOCKED_REASON}"
    )
    next_page.get_by_role("button", name="Blocked", exact=True).click()
    evidence.expect(next_page, "the choice is in the address").to_have_url(re.compile(r"[?&]blocked=true\b"))
    evidence.expect(blocked, "the blocked story stays").to_be_visible()
    evidence.expect(
        next_page.locator(f"[data-story={RUNNING_KEY}]"), "a story that is not blocked leaves"
    ).to_have_count(0)
    next_page.get_by_role("button", name="Not blocked").click()
    evidence.expect(next_page.locator(f"[data-story={RUNNING_KEY}]"), "Not blocked shows it").to_be_visible()
    evidence.expect(blocked, "and drops the blocked one").to_have_count(0)


def test_the_list_follows_a_storys_state_on_disk(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    _write_story(own_server.state, RUNNING_KEY)
    next_page.goto(own_server.url + "/next/workflows")
    _live(next_page, evidence)
    row = next_page.locator(f"[data-story={RUNNING_KEY}]")
    evidence.expect(row, "the story starts unblocked").not_to_contain_text("Blocked:")
    evidence.expect(next_page.locator(f"[data-running={RUNNING_KEY}]"), "and running").to_be_visible()
    _write_story(own_server.state, RUNNING_KEY, blocked="the tester stopped")  # as the tick would
    evidence.expect(row, "the row now says why it stopped").to_contain_text("Blocked: the tester stopped")
    evidence.expect(
        next_page.locator(f"[data-running={RUNNING_KEY}]"), "a blocked story leaves Running"
    ).to_have_count(0)


def test_the_phone_keeps_its_filters_behind_a_button(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    next_page.set_viewport_size({"width": 375, "height": 812})
    next_page.goto(server.url + "/next/workflows")
    evidence.expect(next_page.locator(f"[data-story={STORY_KEY}]"), "the story list shows").to_be_visible()
    stage = next_page.locator("[data-filter=stage]")
    evidence.expect(stage, "the filters are folded away").to_be_hidden()
    next_page.get_by_role("button", name="Filters").click()
    evidence.expect(stage, "Filters unfolds them").to_be_visible()
    sideways = next_page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
    evidence.shot("the phone page does not scroll sideways", held=not sideways)
    assert not sideways
