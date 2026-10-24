"""The Workflows screen in Chromium: the stories to pick from, one story's pipeline and its jobs, and the
card modal's acceptance criteria and workflow section. Each check leaves its screenshot (see
conftest.Evidence)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import STORY_KEY, Evidence, Server

pytestmark = pytest.mark.e2e


def test_the_navigation_says_workflows(signed_in: Page, server: Server, evidence: Evidence) -> None:
    signed_in.goto(f"{server.url}/#/workflows")
    nav = signed_in.get_by_label("Main")
    evidence.expect(nav.get_by_role("link", name="Workflows"), "the tab is named Workflows").to_be_visible()
    evidence.expect(nav.get_by_role("link", name="Jobs"), "no tab is named Jobs").to_have_count(0)
    evidence.expect(signed_in.get_by_role("heading", name="Workflows"), "the page heading").to_be_visible()


def test_the_old_jobs_address_goes_to_workflows(signed_in: Page, server: Server, evidence: Evidence) -> None:
    signed_in.goto(f"{server.url}/#/jobs")
    evidence.expect(signed_in, "#/jobs lands on workflows").to_have_url(re.compile(r"#/workflows$"))


def test_a_story_is_picked_from_the_list(signed_in: Page, server: Server, evidence: Evidence) -> None:
    signed_in.goto(f"{server.url}/#/workflows")
    row = signed_in.locator(f'a.jobrow[data-story="{STORY_KEY}"]')
    evidence.expect(row, "the story is listed").to_contain_text("Board: panel width")
    row.click()
    evidence.expect(signed_in, "the story's own address").to_have_url(
        re.compile(rf"#/workflows/{STORY_KEY}$")
    )
    evidence.expect(
        signed_in.get_by_role("heading", name="Board: panel width"), "its heading"
    ).to_be_visible()


def test_the_pipeline_shows_stages_and_a_lane_per_layer_and_opens_on_the_failed_job(
    signed_in: Page, server: Server, evidence: Evidence, errors: list[str]
) -> None:
    signed_in.goto(f"{server.url}/#/workflows/{STORY_KEY}")
    lanes = signed_in.locator(".wf-lane")
    evidence.expect(lanes, "one lane per layer").to_have_count(2)
    evidence.expect(lanes.nth(0), "layer 01 is built").to_have_class(re.compile(r"\bpassed\b"))
    evidence.expect(lanes.nth(1), "layer 02 stopped").to_have_class(re.compile(r"\bfailed\b"))
    stage = signed_in.locator(".wf-job", has_text="Refine")
    evidence.expect(stage, "the refine stage passed").to_have_class(re.compile(r"\bpassed\b"))
    detail = signed_in.locator("section[aria-live=polite]")
    evidence.expect(detail, "the failed job is open").to_contain_text("Layer 02")
    evidence.expect(detail, "it says why it retried").to_contain_text("no_new_test")
    evidence.shot("nothing logged an error", held=not errors)
    assert errors == []


def test_a_job_shows_what_it_was_given_and_what_it_did(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    signed_in.goto(f"{server.url}/#/workflows/{STORY_KEY}")
    signed_in.locator('.wf-lane[data-node="layer-01"] .wf-job', has_text="Tests").click()
    detail = signed_in.locator("section[aria-live=polite]")
    evidence.expect(detail, "the tester's prompt").to_contain_text(
        "write the failing tests for the drag handle"
    )
    evidence.expect(detail, "its speed").to_contain_text("tok/s")
    detail.locator("details.wf-step summary").first.click()
    evidence.expect(detail, "a step's reasoning opens").to_contain_text("start from the existing board tests")


def test_a_job_without_a_recorded_prompt_says_so(signed_in: Page, server: Server, evidence: Evidence) -> None:
    signed_in.goto(f"{server.url}/#/workflows/{STORY_KEY}/layer-01/build")
    detail = signed_in.locator("section[aria-live=polite]")
    evidence.expect(detail, "an older run says it kept no prompt").to_contain_text(
        "opening prompt not recorded"
    )


def test_the_card_modal_shows_acceptance_criteria_and_links_to_the_workflow(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    signed_in.goto(f"{server.url}/#/board/{STORY_KEY}")
    modal = signed_in.locator("[role='dialog'][aria-modal='true']")
    criteria = modal.get_by_label("Acceptance criteria")
    evidence.expect(criteria, "the criteria have their own section").to_contain_text(
        "the panel width follows"
    )
    evidence.expect(criteria.locator(".criterion"), "both criteria are listed").to_have_count(2)
    workflow = modal.get_by_label("Workflow")
    evidence.expect(workflow, "layer 02's block reason shows").to_contain_text("Full screen toggle")
    workflow.get_by_role("link", name="Open pipeline").click()
    evidence.expect(signed_in, "Open pipeline goes to the story").to_have_url(
        re.compile(rf"#/workflows/{STORY_KEY}$")
    )


def test_a_card_with_no_story_has_no_criteria_section(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    signed_in.goto(f"{server.url}/#/board/AIR-3")
    modal = signed_in.locator("[role='dialog'][aria-modal='true']")
    evidence.expect(
        modal.get_by_role("heading", name="Board: columns fill the page width"), "the modal is open"
    ).to_be_visible()
    evidence.expect(modal.get_by_label("Acceptance criteria"), "no criteria section").to_have_count(0)
    evidence.expect(modal.get_by_label("Workflow"), "no workflow section").to_have_count(0)
