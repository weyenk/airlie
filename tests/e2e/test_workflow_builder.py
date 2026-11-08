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
STORY_NODES = list(yaml.safe_load((WORKFLOWS / "story.yaml").read_text())["workflows"]["story"]["nodes"])
"""The story workflow's nodes, as the shipped file names them."""


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
    form = app_page.locator("[data-node-form]")
    evidence.expect(form, "the new box is selected, its form open").to_be_visible()
    form.locator("[data-input=run]").select_option("story.intake")
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
