"""The Vue app's workflow view for a story in Chromium (/next/workflows/AIR-4): the stages and a lane per
layer, the job open on the failed one, why it retried, what a job was given and how fast it ran, a step's
reasoning, and the phone's top-to-bottom pipeline. Each check leaves its screenshot in
test-results/e2e/evidence/ (conftest.Evidence).

The story is the shared one from conftest: "Board: panel width" (AIR-4), layer 01 built, layer 02 stopped."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Locator, Page

from tests.e2e.conftest import STORY_KEY, Evidence, Server

pytestmark = pytest.mark.e2e

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 375, "height": 812}


def detail(page: Page) -> Locator:
    return page.locator("section[aria-live=polite]")


def open_story(page: Page, server: Server, path: str = "") -> None:
    page.set_viewport_size(DESKTOP)
    page.goto(f"{server.url}/next/workflows/{STORY_KEY}{path}")


def test_the_pipeline_shows_stages_and_a_lane_per_layer_and_opens_on_the_failed_job(
    next_page: Page, server: Server, evidence: Evidence, errors: list[str]
) -> None:
    open_story(next_page, server)
    evidence.expect(
        next_page.get_by_role("heading", name="Board: panel width"), "the story's heading"
    ).to_be_visible()
    lanes = next_page.locator("[data-lane]")
    evidence.expect(lanes, "one lane per layer").to_have_count(2)
    evidence.expect(lanes.nth(0), "layer 01 is built").to_have_attribute("data-status", "passed")
    evidence.expect(lanes.nth(1), "layer 02 stopped").to_have_attribute("data-status", "failed")
    evidence.expect(
        next_page.locator("[data-flow] > button[data-job=refine]"), "the refine stage passed"
    ).to_have_attribute("data-status", "passed")
    evidence.expect(detail(next_page), "the failed job is open").to_contain_text("Layer 02")
    evidence.expect(detail(next_page), "it says why it retried").to_contain_text("no_new_test")
    evidence.expect(
        next_page.locator("[data-lane][data-node=layer-02] [data-job=checks]"),
        "the failed box is the selected one",
    ).to_have_attribute("aria-pressed", "true")
    evidence.shot("nothing logged an error", held=not errors)
    assert errors == []


def test_the_head_says_where_the_story_is_and_why_it_stopped(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(next_page, server)
    evidence.expect(
        next_page.get_by_text("blocked", exact=True), "the stage chip says blocked"
    ).to_be_visible()
    evidence.expect(next_page.get_by_role("alert").first, "the reason it stopped").to_contain_text(
        "its tests still do not test its change"
    )
    evidence.expect(next_page.get_by_role("link", name="Board card"), "a link to the card").to_have_attribute(
        "href", f"/next/board/{STORY_KEY}"
    )
    evidence.expect(
        next_page.locator("[data-stop]"), "this server runs no job for it: no Stop"
    ).to_have_count(0)
    next_page.get_by_role("link", name="Workflows").first.click()
    evidence.expect(next_page, "the back link goes to Workflows").to_have_url(re.compile(r"/next/workflows$"))


def test_a_job_shows_what_it_was_given_and_what_it_did(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(next_page, server)
    next_page.locator('[data-lane][data-node="layer-01"] [data-job=tests]').click()
    evidence.expect(next_page, "the job is in the address").to_have_url(
        re.compile(rf"/next/workflows/{STORY_KEY}/layer-01/tests$")
    )
    evidence.expect(detail(next_page), "the tester's prompt").to_contain_text(
        "write the failing tests for the drag handle"
    )
    evidence.expect(detail(next_page), "its speed").to_contain_text("tok/s")
    detail(next_page).locator("details[data-step] summary").first.click()
    evidence.expect(detail(next_page), "a step's reasoning opens").to_contain_text(
        "start from the existing board tests"
    )


def test_a_job_without_a_recorded_prompt_says_so(next_page: Page, server: Server, evidence: Evidence) -> None:
    open_story(next_page, server, "/layer-01/build")
    evidence.expect(detail(next_page), "an older run says it kept no prompt").to_contain_text(
        "opening prompt not recorded"
    )


def test_a_story_that_does_not_exist_says_so(next_page: Page, server: Server, evidence: Evidence) -> None:
    next_page.goto(f"{server.url}/next/workflows/AIR-999")
    evidence.expect(next_page.get_by_text("No story AIR-999."), "the missing story is named").to_be_visible()


def test_on_a_phone_the_pipeline_runs_top_to_bottom_with_each_layers_jobs_side_by_side(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    next_page.set_viewport_size(PHONE)
    next_page.goto(f"{server.url}/next/workflows/{STORY_KEY}")
    lanes = next_page.locator("[data-vlane]")
    evidence.expect(lanes, "a lane per layer").to_have_count(2)
    evidence.expect(next_page.locator("[data-flow]"), "no sideways flow").to_have_count(0)
    first = lanes.nth(0).locator("[data-job]")
    evidence.expect(first, "three jobs in a layer").to_have_count(3)
    boxes = [first.nth(i).bounding_box() for i in range(3)]
    assert all(b is not None for b in boxes)
    ys = {round(b["y"]) for b in boxes if b}
    xs = [b["x"] for b in boxes if b]
    evidence.shot("the three jobs share a row", held=len(ys) == 1 and xs == sorted(xs))
    assert len(ys) == 1 and xs == sorted(xs)
    first_lane, second_lane = lanes.nth(0).bounding_box(), lanes.nth(1).bounding_box()
    assert first_lane is not None and second_lane is not None
    evidence.shot("the layers stack", held=second_lane["y"] > first_lane["y"])
    assert second_lane["y"] > first_lane["y"]
    doc_width = next_page.evaluate("document.documentElement.scrollWidth")
    evidence.shot("no sideways page scroll", held=doc_width <= PHONE["width"])
    assert doc_width <= PHONE["width"]
