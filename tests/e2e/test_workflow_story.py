"""The Vue app's workflow view for a story in Chromium (/workflows/AIR-4): every step a box (model, code or
milestone), a lane per layer with each try cycle and the divider between them, the now bar, a box's card on
hover and on keyboard focus, the exit row of a stopped story, the job open on the failed box, what a job was
given and how fast it ran, and on a phone the folded list with the sheet a tap opens. Each check leaves its
screenshot in test-results/e2e/evidence/ (conftest.Evidence).

The story is the shared one from conftest: "Board: panel width" (AIR-4), layer 01 built in one try, layer 02
stopped twice (a package returned for a test that tests nothing, then a build the gate sent back)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e.conftest import STORY_KEY, Evidence, Server

pytestmark = pytest.mark.e2e

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 375, "height": 812}


def detail(page: Page) -> Locator:
    return page.locator("section[aria-label=Job]")


def open_story(page: Page, server: Server, path: str = "") -> None:
    page.set_viewport_size(DESKTOP)
    page.goto(f"{server.url}/workflows/{STORY_KEY}{path}")


def live_story(state: Path, key: str, *, quiet_s: float) -> None:
    """A story whose first layer's build is on its coder, last heard from `quiet_s` ago: live when that is
    short, one that was left (drawn stopped) when it is long."""
    at = time.time() - quiet_s
    story = state / "stories" / key
    story.mkdir(parents=True)
    layer = {"slug": "seam", "title": "Resizable panel", "scope": "x", "brief": "x", "acceptance": ["AC1"]}
    (story / "state.json").write_text(
        json.dumps(
            {
                "card": {"key": key, "title": "Live story"},
                "repo": "/r",
                "stage": "layers",
                "story": {"title": "Live story", "summary": "s", "acceptance": []},
                "layers": [layer],
            }
        )
    )

    def ev(rows: list[dict[str, object]]) -> str:
        return "".join(json.dumps(r) + "\n" for r in rows)

    layer_events = [
        {"ts": at - 90, "kind": "story_planned"},
        {"ts": at - 80, "kind": "package_start", "layer": "01", "attempt": 1},
        {"ts": at - 70, "kind": "package_checks_start", "layer": "01"},
        {"ts": at - 60, "kind": "package_ready", "layer": "01", "studio_run": "run-live"},
        {"ts": at - 50, "kind": "layer_start", "layer": "01", "studio_run": "run-live"},
        {"ts": at - 40, "kind": "package_placed", "layer": "01", "paths": ["tests/x.test.js"]},
    ]
    (story / "events.jsonl").write_text(ev(layer_events))
    run = state / "runs" / "run-live"
    run.mkdir(parents=True)
    step = {"kind": "step", "team_id": "build", "step": 1, "tool": "read_file", "ok": True, "role": "coder"}
    (run / "events.jsonl").write_text(
        ev(
            [
                {"ts": at - 30, "kind": "subtask_start", "subtask": "build", "role": "coder"},
                {"ts": at, **step, "prompt_tokens": 900, "completion_tokens": 90, "gen_ms": 1500.0},
            ]
        )
    )


def test_the_pipeline_draws_every_step_as_a_box_and_opens_on_the_failed_one(
    app_page: Page, server: Server, evidence: Evidence, errors: list[str]
) -> None:
    open_story(app_page, server)
    evidence.expect(
        app_page.get_by_role("heading", name="Board: panel width"), "the story's heading"
    ).to_be_visible()
    lanes = app_page.locator("[data-lane]")
    evidence.expect(lanes, "one lane per layer").to_have_count(2)
    evidence.expect(lanes.nth(0), "layer 01 is built").to_have_attribute("data-status", "passed")
    evidence.expect(lanes.nth(1), "layer 02 stopped").to_have_attribute("data-status", "failed")
    evidence.expect(
        app_page.locator("[data-flow] [data-node=refine][data-job=refine]"), "the refine stage passed"
    ).to_have_attribute("data-status", "passed")
    evidence.expect(
        app_page.locator("[data-flow] [data-node=intake][data-job=intake]"), "intake is a code box"
    ).to_have_attribute("data-shape", "code")
    evidence.expect(
        app_page.locator("[data-flow] [data-node=planned][data-job=planned]"), "Planned is a milestone"
    ).to_have_attribute("data-shape", "milestone")
    layer_one = app_page.locator("[data-lane][data-node=layer-01]")
    evidence.expect(layer_one.locator("[data-job]"), "five jobs in a layer's try").to_have_count(5)
    boxes = layer_one.locator("[data-job]")
    jobs = [boxes.nth(i).get_attribute("data-job") for i in range(5)]
    want = ["package", "checks", "place", "build", "squash"]
    evidence.shot("they run package, checks, place, build, squash", held=jobs == want)
    assert jobs == want
    shapes = [boxes.nth(i).get_attribute("data-shape") for i in range(5)]
    shapes_want = ["model", "code", "code", "model", "code"]
    evidence.shot("a model box, airlie's own checks, then the build", held=shapes == shapes_want)
    assert shapes == shapes_want
    evidence.expect(detail(app_page), "the failed job is open").to_contain_text("Layer 02 / Build")
    evidence.expect(detail(app_page), "it says why it retried").to_contain_text("gate_failed")
    evidence.expect(
        app_page.locator("[data-lane][data-node=layer-02] [data-job=build]"),
        "the failed box is the selected one",
    ).to_have_attribute("aria-pressed", "true")
    evidence.shot("nothing logged an error", held=not errors)
    assert errors == []


def test_a_layer_that_stopped_shows_each_try_cycle_with_a_divider_between(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(app_page, server)
    lane = app_page.locator("[data-lane][data-node=layer-02]")
    evidence.expect(lane.locator("[data-row]"), "layer 02 has two try cycles").to_have_count(2)
    evidence.expect(lane.locator("[data-divider]"), "with the stop between them").to_have_count(1)
    evidence.expect(lane.locator("[data-divider]"), "the divider says it was a block").to_contain_text(
        "blocked"
    )
    evidence.expect(
        lane.locator("[data-row-id=c1] [data-job=c1-checks]"), "the first cycle's boxes keep their own ids"
    ).to_have_count(1)
    evidence.expect(
        app_page.locator("[data-lane][data-node=layer-01] [data-row]"), "a layer built in one try has one row"
    ).to_have_count(1)


def test_the_now_bar_names_the_step_the_story_stopped_at(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(app_page, server)
    now = app_page.locator("[data-now]")
    evidence.expect(now, "a story at rest is not running").to_have_attribute("data-now-status", "idle")
    evidence.expect(now, "the bar names where it is blocked").to_contain_text(
        "Blocked at Build lane › Layer 02 › build 1 · red"
    )


def test_the_now_bar_names_the_running_step_and_its_box_is_lit(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    live_story(own_server.state, "AIR-41", quiet_s=5)
    app_page.set_viewport_size(DESKTOP)
    app_page.goto(f"{own_server.url}/workflows/AIR-41")
    now = app_page.locator("[data-now]")
    evidence.expect(now, "the story is running").to_have_attribute("data-now-status", "running")
    evidence.expect(now, "the bar names the step").to_contain_text("Running")
    evidence.expect(now, "and the layer it is in").to_contain_text("Layer 01")
    evidence.expect(now, "and the coder's step count").to_contain_text("step 1")
    build = app_page.locator("[data-lane][data-node=layer-01] [data-job=build]")
    evidence.expect(build, "the box the bar names is the one that is lit").to_have_attribute(
        "data-status", "running"
    )


def test_the_now_bar_says_stopped_for_a_story_nobody_works_on(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    live_story(own_server.state, "AIR-42", quiet_s=2 * 3600)
    app_page.set_viewport_size(DESKTOP)
    app_page.goto(f"{own_server.url}/workflows/AIR-42")
    now = app_page.locator("[data-now]")
    evidence.expect(now, "a quiet story is drawn stopped").to_have_attribute("data-now-status", "stopped")
    evidence.expect(now, "the bar says where").to_contain_text("Stopped at")
    evidence.expect(
        app_page.locator("[data-lane][data-node=layer-01] [data-job=build]"), "its build is not drawn running"
    ).to_have_attribute("data-status", "stopped")


def test_hovering_the_build_box_opens_its_card_with_coder_reviewer_and_gate(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(app_page, server)
    build = app_page.locator("[data-lane][data-node=layer-01] [data-job=build]")
    build.scroll_into_view_if_needed()  # the flow scrolls sideways: let it settle before the pointer arrives
    app_page.wait_for_timeout(300)
    app_page.mouse.move(5, 5)
    build.hover()
    card = app_page.locator("[data-card]")
    evidence.expect(card, "the card opens").to_be_visible()
    evidence.expect(card, "it is the Build's").to_contain_text("Build")
    for step in ("coder", "reviewer", "gate"):
        evidence.expect(card, f"it lists the {step}").to_contain_text(step)
    app_page.mouse.move(5, 5)
    evidence.expect(card, "moving away closes it").to_have_count(0)


def test_keyboard_focus_on_a_box_opens_its_card(app_page: Page, server: Server, evidence: Evidence) -> None:
    open_story(app_page, server)
    app_page.locator("[data-lane][data-node=layer-01] [data-job=build]").focus()
    card = app_page.locator("[data-card]")
    evidence.expect(card, "focus opens the card").to_be_visible()
    evidence.expect(card, "the same card").to_contain_text("reviewer")
    app_page.keyboard.press("Escape")
    evidence.expect(card, "Escape closes it").to_have_count(0)


def test_the_exit_row_shows_how_the_blocked_story_left(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(app_page, server)
    row = app_page.locator("[data-exit=blocked]")
    evidence.expect(row, "a blocked story ends in an exit row").to_be_visible()
    evidence.expect(row, "it says Blocked").to_contain_text("Blocked")
    evidence.expect(row, "and where").to_contain_text("Layer 02")
    evidence.expect(row, "and why").to_contain_text("the gate stays red")
    for job in ("diagnose", "fix", "card"):
        box = row.locator(f"[data-node=exit][data-job={job}]")
        evidence.expect(box, f"the {job} box follows").to_have_count(1)
    row.locator("[data-job=card]").click()
    evidence.expect(app_page, "an exit box opens its detail").to_have_url(
        re.compile(rf"/workflows/{STORY_KEY}/exit/card$")
    )


def test_a_running_story_has_no_exit_row(app_page: Page, own_server: Server, evidence: Evidence) -> None:
    live_story(own_server.state, "AIR-41", quiet_s=5)
    app_page.set_viewport_size(DESKTOP)
    app_page.goto(f"{own_server.url}/workflows/AIR-41")
    evidence.expect(app_page.locator("[data-now]"), "the page has loaded").to_be_visible()
    evidence.expect(app_page.locator("[data-exit-row]"), "nothing has ended").to_have_count(0)


def test_the_head_says_where_the_story_is_and_why_it_stopped(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(app_page, server)
    evidence.expect(
        app_page.get_by_text("blocked", exact=True), "the stage chip says blocked"
    ).to_be_visible()
    evidence.expect(app_page.get_by_role("alert").first, "the reason it stopped").to_contain_text(
        "the gate stays red"
    )
    evidence.expect(app_page.get_by_role("link", name="Board card"), "a link to the card").to_have_attribute(
        "href", f"/board/{STORY_KEY}"
    )
    evidence.expect(app_page.locator("[data-stop]"), "this server runs no job for it: no Stop").to_have_count(
        0
    )
    app_page.get_by_role("link", name="Workflows").first.click()
    evidence.expect(app_page, "the back link goes to Workflows").to_have_url(re.compile(r"/workflows$"))


def test_a_job_shows_what_it_was_given_and_what_it_did(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(app_page, server)
    app_page.locator('[data-lane][data-node="layer-01"] [data-job=package]').click()
    evidence.expect(app_page, "the job is in the address").to_have_url(
        re.compile(rf"/workflows/{STORY_KEY}/layer-01/package$")
    )
    evidence.expect(detail(app_page), "the architect's prompt").to_contain_text(
        "write the failing tests for the drag handle"
    )
    evidence.expect(detail(app_page), "its speed").to_contain_text("tok/s")
    step = detail(app_page).locator("details[data-step]").first
    evidence.expect(step, "the step starts closed").not_to_have_attribute("open", "")
    evidence.expect(step.locator("pre"), "a closed step holds no text of its own").to_have_count(0)
    step.locator("summary").click()
    # asserted without a screenshot in between: a full-page capture resizes the viewport and, one run in
    # six here, closes the step that was just opened (see docs/bench/2026-11-02-pipeline-screen-check.md)
    expect(step).to_have_attribute("open", "")
    expect(step.locator("pre").first).to_be_visible()
    evidence.expect(step, "a step's reasoning opens").to_contain_text("start from the existing board tests")


def test_an_earlier_try_cycles_box_opens_its_detail(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(app_page, server)
    app_page.locator("[data-lane][data-node=layer-02] [data-job=c1-checks]").click()
    evidence.expect(app_page, "the earlier try is in the address").to_have_url(
        re.compile(rf"/workflows/{STORY_KEY}/layer-02/c1-checks$")
    )
    evidence.expect(detail(app_page), "its detail is open").to_contain_text("Layer 02 / checks")
    evidence.expect(detail(app_page), "it says why that try retried").to_contain_text("no_new_test")


def test_a_job_without_a_recorded_prompt_says_so(app_page: Page, server: Server, evidence: Evidence) -> None:
    open_story(app_page, server, "/layer-01/build")
    evidence.expect(detail(app_page), "an older run says it kept no prompt").to_contain_text(
        "opening prompt not recorded"
    )


def test_one_of_airlies_own_checks_says_it_ran_no_model(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(app_page, server, "/layer-01/place")
    evidence.expect(detail(app_page), "a code box has no prompt").to_contain_text("no model")


def test_a_story_that_does_not_exist_says_so(app_page: Page, server: Server, evidence: Evidence) -> None:
    app_page.goto(f"{server.url}/workflows/AIR-999")
    evidence.expect(app_page.get_by_text("No story AIR-999."), "the missing story is named").to_be_visible()


def test_on_a_phone_the_list_folds_what_is_done_and_opens_the_step_that_stopped(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size(PHONE)
    app_page.goto(f"{server.url}/workflows/{STORY_KEY}")
    evidence.expect(app_page.locator("[data-flow]"), "no sideways flow").to_have_count(0)
    evidence.expect(app_page.locator("[data-vlane]"), "the old phone layout is gone").to_have_count(0)
    evidence.expect(app_page.locator("[data-now]"), "the now bar is on top").to_contain_text("Blocked at")
    folded = app_page.locator("[data-folded=plan]")
    evidence.expect(folded, "the finished plan lane is one folded row").to_be_visible()
    evidence.expect(folded, "it says what it holds").to_contain_text("Planned")
    evidence.expect(folded.locator("[data-chip]").first, "with a chip per step").to_be_visible()
    evidence.expect(app_page.locator("[data-node=layer-01]"), "a built layer is one row").to_have_attribute(
        "data-status", "passed"
    )
    opened = app_page.locator("[data-open][data-node=layer-02]")
    evidence.expect(opened, "only the layer that stopped is open").to_be_visible()
    evidence.expect(opened.locator("[data-job-row]"), "with a row per job").to_have_count(5)
    earlier = opened.locator("[data-earlier]")
    evidence.expect(earlier, "and its earlier try, folded").to_have_count(1)
    evidence.expect(app_page.locator("[data-sheet]"), "no sheet until a tap").to_have_count(0)
    doc_width = app_page.evaluate("document.documentElement.scrollWidth")
    evidence.shot("no sideways page scroll", held=doc_width <= PHONE["width"])
    assert doc_width <= PHONE["width"]


def test_on_a_phone_a_tap_on_layer_02s_build_opens_the_sheet_and_closing_returns_to_the_list(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size(PHONE)
    app_page.goto(f"{server.url}/workflows/{STORY_KEY}")
    app_page.locator("[data-open][data-node=layer-02] [data-job=build]").click()
    sheet = app_page.locator("[data-sheet]")
    evidence.expect(sheet, "the sheet opens").to_be_visible()
    evidence.expect(app_page, "the job is in the address").to_have_url(
        re.compile(rf"/workflows/{STORY_KEY}/layer-02/build$")
    )
    evidence.expect(sheet.locator("[data-card]"), "it holds the card").to_contain_text("reviewer")
    evidence.expect(sheet.locator("section[aria-label=Job]"), "and the job's detail").to_contain_text(
        "Layer 02 / Build"
    )
    evidence.expect(sheet.locator("details[data-step]").first, "with its trace").to_be_visible()
    app_page.get_by_role("button", name="Close").click()
    evidence.expect(sheet, "closing hides the sheet").to_have_count(0)
    evidence.expect(app_page, "and returns to the story's own address").to_have_url(
        re.compile(rf"/workflows/{STORY_KEY}$")
    )
    evidence.expect(app_page.locator("[data-open][data-node=layer-02]"), "the list is back").to_be_visible()


def test_a_link_to_a_job_that_does_not_exist_opens_the_default_one(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_story(app_page, server, "/layer-09/tests")
    evidence.expect(detail(app_page).locator("h2"), "the failed job is open").to_have_text("Layer 02 / Build")
    evidence.expect(
        app_page.locator("[data-lane][data-node=layer-02] [data-job=build]"), "and selected"
    ).to_have_attribute("aria-pressed", "true")


def test_a_120_character_layer_title_is_cut_short_inside_its_lane(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    long_title = "A very long layer title " * 5
    long_title = long_title[:120]
    story = own_server.state / "stories" / "AIR-40"
    story.mkdir(parents=True)
    layer = {"slug": "long", "title": long_title, "scope": "x", "brief": "x", "acceptance": ["AC1"]}
    (story / "state.json").write_text(
        json.dumps({"card": {"key": "AIR-40", "title": "Long"}, "stage": "layers", "layers": [layer]})
    )
    (story / "events.jsonl").write_text(
        json.dumps({"ts": 1.0, "kind": "package_start", "layer": "01", "attempt": 1}) + "\n"
    )
    app_page.set_viewport_size(DESKTOP)
    app_page.goto(f"{own_server.url}/workflows/AIR-40")
    lane = app_page.locator("[data-lane]")
    head = lane.locator("[data-title]")
    evidence.expect(head, "the full title is in its title attribute").to_have_attribute(
        "title", f"Layer 01 · {long_title}"
    )
    cut = head.evaluate("e => e.scrollWidth > e.clientWidth")
    evidence.shot("the text is cut short", held=bool(cut))
    assert cut
    lane_box = lane.bounding_box()
    assert lane_box is not None
    count = lane.locator("[data-job]").count()
    boxes = [lane.locator("[data-job]").nth(i).bounding_box() for i in range(count)]
    inside = count == 5 and all(
        b is not None and b["x"] + b["width"] <= lane_box["x"] + lane_box["width"] + 0.5 for b in boxes
    )
    evidence.shot("the five boxes stay within the lane", held=inside)
    assert inside
    evidence.expect(lane.locator("[data-acceptance]"), "the acceptance id stays on one line").to_have_text(
        "AC1"
    )
