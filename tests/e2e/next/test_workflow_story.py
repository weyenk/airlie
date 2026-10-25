"""The Vue app's workflow view for a story in Chromium (/next/workflows/AIR-4): the stages and a lane per
layer, the job open on the failed one, why it retried, what a job was given and how fast it ran, a step's
reasoning, and the phone's top-to-bottom pipeline. Each check leaves its screenshot in
test-results/e2e/evidence/ (conftest.Evidence).

The story is the shared one from conftest: "Board: panel width" (AIR-4), layer 01 built, layer 02 stopped."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Locator, Page

from tests.e2e.conftest import STORY_KEY, Evidence, Server

pytestmark = pytest.mark.e2e

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 375, "height": 812}


def detail(page: Page) -> Locator:
    return page.locator("section[aria-label=Job]")


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
    step = detail(next_page).locator("details[data-step]").first
    evidence.expect(step, "the step starts closed").not_to_have_attribute("open", "")
    evidence.expect(step.locator("pre"), "a closed step holds no text of its own").to_have_count(0)
    step.locator("summary").click()
    evidence.expect(step, "a click opens the step").to_have_attribute("open", "")
    evidence.expect(step.locator("pre").first, "its reasoning is on show").to_be_visible()
    evidence.expect(step, "a step's reasoning opens").to_contain_text("start from the existing board tests")


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


def test_a_link_to_a_job_that_does_not_exist_opens_the_default_one(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(next_page, server, "/layer-09/tests")
    evidence.expect(detail(next_page).locator("h2"), "the failed job is open").to_have_text(
        "Layer 02 / Test checks"
    )
    evidence.expect(
        next_page.locator("[data-lane][data-node=layer-02] [data-job=checks]"), "and selected"
    ).to_have_attribute("aria-pressed", "true")


def test_a_120_character_layer_title_is_cut_short_inside_its_lane(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    long_title = "A very long layer title " * 5
    long_title = long_title[:120]
    story = own_server.state / "stories" / "AIR-40"
    story.mkdir(parents=True)
    layer = {"slug": "long", "title": long_title, "scope": "x", "brief": "x", "acceptance": ["AC1"]}
    (story / "state.json").write_text(
        json.dumps({"card": {"key": "AIR-40", "title": "Long"}, "stage": "layers", "layers": [layer]})
    )
    next_page.set_viewport_size(DESKTOP)
    next_page.goto(f"{own_server.url}/next/workflows/AIR-40")
    lane = next_page.locator("[data-lane]")
    head = lane.locator("[data-title]")
    evidence.expect(head, "the full title is in its title attribute").to_have_attribute(
        "title", f"Layer 01 \u00b7 {long_title}"
    )
    cut = head.evaluate("e => e.scrollWidth > e.clientWidth")
    evidence.shot("the text is cut short", held=bool(cut))
    assert cut
    lane_box = lane.bounding_box()
    assert lane_box is not None
    boxes = [lane.locator("[data-job]").nth(i).bounding_box() for i in range(3)]
    inside = all(
        b is not None and b["x"] + b["width"] <= lane_box["x"] + lane_box["width"] + 0.5 for b in boxes
    )
    evidence.shot("the three boxes stay within the lane", held=inside)
    assert inside
    evidence.expect(lane.locator("[data-acceptance]"), "the acceptance id stays on one line").to_have_text(
        "AC1"
    )
