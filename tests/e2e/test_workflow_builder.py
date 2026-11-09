"""The workflow builder in Chromium: a story card's Workflow field opens the builder (`＋ Build new…`), a
box placed from the palette and given its run validates, Save draft names it and returns to the card with the
draft chosen and the typed title kept; and the shipped story workflow drawn as a box per node. Each check
leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence).

The server is `workflows_server`: its own board (the test saves a draft), its config with the shipped workflow
files (story, layer) as airlie.yaml includes them."""

from __future__ import annotations

import re

import pytest
import yaml
from playwright.sync_api import Page

from tests.e2e.conftest import WORKFLOWS, Evidence, Server

pytestmark = pytest.mark.e2e

DESKTOP = {"width": 1280, "height": 900}
TITLE = "Fix the login loop"
STORY = yaml.safe_load((WORKFLOWS / "story.yaml").read_text())["workflows"]["story"]["nodes"]
STORY_NODES = list(STORY)
"""The story workflow's nodes, as the shipped file names them."""
STORY_LINES = sum(len(node.get("to", [])) for node in STORY.values())
"""The story workflow's lines: a `to` entry each."""


def test_build_new_from_a_card_saves_a_draft_and_returns_with_it_chosen(
    app_page: Page, workflows_server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size(DESKTOP)
    app_page.goto(workflows_server.url + "/board")
    app_page.get_by_role("button", name="Create").click()
    dialog = app_page.get_by_role("dialog")
    evidence.expect(dialog, "Create opens").to_be_visible()
    dialog.get_by_label("Title").fill(TITLE)
    workflow = dialog.locator("[data-workflow]")
    evidence.expect(workflow, "a story has a Workflow field").to_be_visible()
    workflow.select_option("__new__")
    evidence.expect(app_page, "Build new… opens the builder on an empty canvas").to_have_url(
        re.compile(r"/workflows/build/new\?card=")
    )

    app_page.locator("[data-palette-type=check]").click()
    box = app_page.locator("[data-box][data-type=check]")
    evidence.expect(box, "a click on the palette's check adds a check box").to_have_count(1)
    evidence.expect(
        app_page.locator("[data-box-card]"), "the new box is selected, a small card shows"
    ).to_be_visible()
    app_page.locator("[data-workflow-edit]").click()
    settings = app_page.locator("[data-workflow-modal]")
    evidence.expect(settings, "the Workflow button opens the workflow's own fields").to_be_visible()
    settings.locator("[data-workflow-add=inputs]").fill("card")
    settings.locator("[data-workflow-add=inputs]").press("Enter")
    evidence.expect(
        settings.locator("[data-chip=card]"), "the card is an input the intake check reads"
    ).to_be_visible()
    settings.locator("[data-workflow-done]").click()
    box.dblclick()
    modal = app_page.locator("[data-node-modal]")
    evidence.expect(modal, "a double-click opens the node's fields").to_be_visible()
    modal.locator("[data-catalog-pick] [data-check='story.intake']").click()
    evidence.expect(
        modal.locator("[data-catalog-pick] [data-check='story.intake']"),
        "the intake check is picked from the catalog",
    ).to_have_attribute("aria-pressed", "true")
    missing = modal.locator("[data-input-missing=card]")
    evidence.expect(missing, "the check reads the card, which the box does not list yet").to_be_visible()
    missing.click()
    evidence.expect(modal.locator("[data-input-name=card]"), "the box now reads the card").to_be_checked()
    modal.locator("[data-node-done]").click()
    evidence.expect(modal, "Done closes the modal").to_have_count(0)
    evidence.expect(app_page.locator("[data-dirty]"), "the edit is unsaved").to_be_visible()

    app_page.locator("[data-validate]").click()
    evidence.expect(app_page.locator("[data-no-problems]"), "Validate finds no problems").to_be_visible()
    evidence.expect(app_page.locator("[data-problem]"), "and lists none").to_have_count(0)

    app_page.locator("[data-save]").click()
    save = app_page.locator("[data-save-dialog]")
    evidence.expect(save, "Save draft asks for a name").to_be_visible()
    save.locator("[data-save-name]").fill("mini")
    save.locator("[data-save-type]").fill("bug")
    save.locator("[data-save-confirm]").click()

    evidence.expect(app_page, "saving returns to the card").to_have_url(re.compile(r"/board\?create="))
    dialog = app_page.get_by_role("dialog")
    evidence.expect(dialog.get_by_label("Title"), "with the typed title kept").to_have_value(TITLE)
    workflow = dialog.locator("[data-workflow]")
    evidence.expect(workflow, "and the draft chosen").to_have_value("mini")
    evidence.expect(workflow.locator("option:checked"), "shown as a draft").to_have_text("mini (draft)")


def test_the_story_workflow_is_drawn_a_box_per_node(
    app_page: Page, workflows_server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size(DESKTOP)
    app_page.goto(workflows_server.url + "/workflows/build/story")
    canvas = app_page.locator("[data-canvas]")
    evidence.expect(canvas.locator("[data-box]"), f"a box per story node ({len(STORY_NODES)})").to_have_count(
        len(STORY_NODES)
    )
    for node in ("intake", "amigos", "review"):
        evidence.expect(canvas.locator(f"[data-box][data-node={node}]"), f"{node} is a box").to_be_visible()
    kind = app_page.locator("[data-workflow-type]")
    evidence.expect(kind, "the workflow's type shows").to_have_text("story")
    evidence.expect(
        canvas.locator("[data-port=out][data-node=base]"), "base shows its output port"
    ).to_be_visible()
    evidence.expect(canvas.locator("[data-edge]"), f"a line per to entry ({STORY_LINES})").to_have_count(
        STORY_LINES
    )
    evidence.expect(
        canvas.locator("[data-edge-label][data-label-fits=true]").first,
        "a label the room takes is drawn above its line",
    ).to_be_visible()
    line = "[data-edge-hit][data-from=story_card][data-to=survey]"
    hidden = canvas.locator(f"g:has(> {line}) > [data-edge-label]")
    evidence.expect(hidden, "a label too long for the room between its boxes is hidden").to_be_hidden()
    # the line is far to the right in the wider layout: bring it into the pane before hovering
    canvas.locator(line).scroll_into_view_if_needed()
    # a point three quarters along the line, past where it leaves story_card beside its other lines
    point = canvas.locator(line).evaluate(
        "p => { const at = p.getPointAtLength(p.getTotalLength() * 0.75); const m = p.getScreenCTM();"
        " return { x: at.x * m.a + m.e, y: at.y * m.d + m.f }; }"
    )
    app_page.mouse.move(point["x"], point["y"])
    evidence.expect(hidden, "and shows while its line is hovered").to_be_visible()
    evidence.expect(
        canvas.locator("[data-edge-label-pop][data-from=story_card][data-to=survey]"),
        "over the boxes, on a card",
    ).to_have_text(re.compile(r"^\s*if not is_story_card and .+ · order\s*$"))
    app_page.mouse.move(0, 0)
    pane = app_page.locator("[data-canvas-pane]")
    wide = pane.evaluate("el => el.scrollWidth > el.clientWidth")
    assert wide, "the story is wider than the pane"
    evidence.shot("the story is wider than the pane: it scrolls, its scrollbar drawn")
    evidence.expect(
        app_page.locator("[data-canvas-more]"), "the pane fades at its right edge: more is off-screen"
    ).to_be_visible()
    last = canvas.locator("[data-box][data-node=review]")
    last.scroll_into_view_if_needed()
    evidence.expect(last, "the last column's box is reached by scrolling").to_be_in_viewport()


def test_a_bundle_box_opens_its_workflow_in_place_with_a_breadcrumb_back(
    app_page: Page, workflows_server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size(DESKTOP)
    app_page.goto(workflows_server.url + "/workflows/build/story")
    bundle = app_page.locator("[data-box][data-shape=bundle][data-node=build_layers]")
    evidence.expect(bundle, "build_layers is a bundle box").to_be_visible()
    # an unsaved edit first: a box moved, which the breadcrumb back must keep
    intake = app_page.locator("[data-box][data-node=intake]")
    before = intake.get_attribute("style")
    at = intake.bounding_box()
    assert at is not None
    app_page.mouse.move(at["x"] + at["width"] / 2, at["y"] + at["height"] / 2)
    app_page.mouse.down()
    app_page.mouse.move(at["x"] + at["width"] / 2 + 8, at["y"] + at["height"] / 2 + 160, steps=8)
    app_page.mouse.up()
    evidence.expect(app_page.locator("[data-dirty]"), "moving intake is an unsaved edit").to_be_visible()
    moved = intake.get_attribute("style")
    assert moved != before, "intake moved"
    evidence.shot("intake moved", intake)
    bundle.dblclick()
    evidence.expect(app_page, "a double-click opens the bundled workflow").to_have_url(
        re.compile(r"/workflows/build/layer")
    )
    crumb = app_page.locator("[data-breadcrumb]")
    evidence.expect(crumb, "a breadcrumb leads back").to_have_text(re.compile("story"))
    evidence.expect(app_page.locator("[data-workflow-type]"), "layer is a bundle").to_have_text("bundle")
    crumb.click()
    evidence.expect(app_page, "the breadcrumb returns to story").to_have_url(
        re.compile(r"/workflows/build/story")
    )
    evidence.expect(app_page.locator("[data-canvas] [data-box]"), "with its boxes").to_have_count(
        len(STORY_NODES)
    )
    evidence.expect(app_page.locator("[data-dirty]"), "and the unsaved edit kept").to_be_visible()
    evidence.expect(intake, "intake where it was moved").to_have_attribute("style", moved or "")
