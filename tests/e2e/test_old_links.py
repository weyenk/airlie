"""Links made for the old app (`/#/board/AIR-17`) and its `/next` home land on the clean path in Chromium.
Each check leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import STORY_KEY, TOPO_LIVE, Evidence, Server

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
    evidence.expect(
        app_page.get_by_role("heading", name="Workflows"), "the app drew, not a 503 page"
    ).to_be_visible()


def test_the_navigation_says_workflows(app_page: Page, server: Server, evidence: Evidence) -> None:
    app_page.goto(f"{server.url}/workflows")
    nav = app_page.get_by_label("Main").first
    evidence.expect(nav.get_by_role("link", name="Workflows"), "the tab is named Workflows").to_be_visible()
    evidence.expect(nav.get_by_role("link", name="Jobs"), "no tab is named Jobs").to_have_count(0)


def test_the_old_jobs_addresses_go_to_workflows(app_page: Page, server: Server, evidence: Evidence) -> None:
    app_page.goto(f"{server.url}/#/jobs")
    evidence.expect(app_page, "#/jobs lands on workflows").to_have_url(re.compile(r"/workflows$"))
    app_page.goto(f"{server.url}/#/jobs/story-{STORY_KEY}")
    evidence.expect(app_page, "a story's job lands on its workflow").to_have_url(
        re.compile(rf"/workflows/{STORY_KEY}$")
    )
    evidence.expect(app_page.locator("[data-lane]").first, "the workflow drew").to_be_visible()


def test_an_old_topology_link_lands_on_the_runs_workflow(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    app_page.goto(f"{server.url}/#/topology/{TOPO_LIVE}")
    evidence.expect(app_page, "topology became the run's workflow").to_have_url(
        re.compile(rf"/workflows/runs/{TOPO_LIVE}$")
    )
    evidence.expect(app_page.locator("[data-stage]").first, "the run drew").to_be_visible()


def test_a_running_run_moves_when_its_log_grows(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    app_page.goto(f"{own_server.url}/workflows/runs/{TOPO_LIVE}")
    coder = app_page.locator("[data-stage] button[data-job=fix]")
    docs = app_page.locator("[data-stage] button[data-job=note]")
    evidence.expect(coder, "the coder is working").to_have_attribute("data-status", "running")
    evidence.expect(docs, "the docs subtask waits").not_to_have_attribute("data-status", "running")
    log = own_server.state / "runs" / TOPO_LIVE / "events.jsonl"
    more = [
        {
            "ts": 1792750007.0,
            "run_id": TOPO_LIVE,
            "kind": "subtask_done",
            "subtask": "fix",
            "role": "coder",
            "ok": True,
            "stop_reason": "done",
            "steps": 2,
        },
        {
            "ts": 1792750008.0,
            "run_id": TOPO_LIVE,
            "kind": "subtask_start",
            "subtask": "note",
            "role": "docs",
            "writes": False,
        },
    ]
    with log.open("a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in more))
    evidence.expect(coder, "the coder is done").to_have_attribute("data-status", "passed")
    evidence.expect(docs, "the docs subtask works now").to_have_attribute("data-status", "running")
