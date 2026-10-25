"""The Vue app's card modal and Create in Chromium (/board/AIR-4): a centred card over the dimmed board,
closed by Escape, the backdrop or the X, with focus kept inside and handed back; every section edited where it
is shown; the repository field and its owner/name rule; a new child taking its parent's repository; and the
phone folding. Each check leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence).

The board is the shared CARDS from conftest: "Board: panel width" is AIR-4 (Approved, a story under AIR-2, in
weyenk/airlie, with a built-and-stopped story behind it)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Locator, Page

from airlie.board.store import BoardStore
from tests.e2e.conftest import Evidence, Server

pytestmark = pytest.mark.e2e

KEY = "AIR-4"
TITLE = "Board: panel width"
DESKTOP = {"width": 1280, "height": 900}


def modal(page: Page) -> Locator:
    return page.get_by_role("dialog")


def overlay(page: Page) -> Locator:
    return page.locator("[data-overlay]")


def open_card(page: Page, server: Server, key: str = KEY) -> Locator:
    """Sign in at desktop width, land on the board, and open the card by clicking it."""
    page.set_viewport_size(DESKTOP)
    page.goto(f"{server.url}/board")
    page.locator(f"[data-item={key}]").click()
    dialog = modal(page)
    dialog.wait_for(state="visible")
    return dialog


def board(server: Server) -> BoardStore:
    return BoardStore(server.state / "board.sqlite")


def change(server: Server, **fields: Any) -> None:
    store = board(server)
    try:
        store.update(KEY, **fields)
    finally:
        store.close()


def stored(server: Server) -> Any:
    store = board(server)
    try:
        return store.get(KEY)
    finally:
        store.close()


def focused_key(page: Page) -> str:
    return str(page.evaluate("() => document.activeElement?.dataset?.item ?? ''"))


def test_clicking_a_card_opens_a_centred_modal_over_the_dimmed_board(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, server)
    evidence.expect(dialog, "the card modal opens").to_be_visible()
    evidence.expect(app_page, "at the card's own address").to_have_url(re.compile(rf"/board/{KEY}$"))
    evidence.expect(overlay(app_page), "the board is dimmed behind it").to_be_visible()
    dim = overlay(app_page).evaluate("o => getComputedStyle(o).backgroundColor")
    evidence.shot("the backdrop is a translucent black", overlay(app_page), held="0.6" in dim)
    assert "0.6" in dim, dim
    grid = app_page.locator("[data-columns]").bounding_box()
    assert grid is not None
    evidence.shot("the columns keep their full width", held=grid["width"] >= 1280 - 40)
    assert grid["width"] >= 1280 - 40, grid
    evidence.expect(dialog, "the modal shows the card's key").to_contain_text(KEY)
    evidence.expect(dialog.get_by_role("heading", name=TITLE), "and its title").to_be_visible()
    evidence.expect(dialog, "and its status").to_contain_text("Approved")


def test_the_modal_is_centred_over_the_board(app_page: Page, server: Server, evidence: Evidence) -> None:
    dialog = open_card(app_page, server)
    box = dialog.bounding_box()
    assert box is not None
    centred = abs((box["x"] + box["width"] / 2) - 640) < 40
    evidence.shot("the modal is centred on the page", dialog, held=centred)
    assert centred, box


@pytest.mark.parametrize("how", ["the X button", "Escape", "a click on the backdrop"])
def test_closing_restores_the_board_and_returns_focus_to_the_card(
    app_page: Page, server: Server, evidence: Evidence, how: str
) -> None:
    dialog = open_card(app_page, server)
    if how == "the X button":
        dialog.get_by_role("button", name="Close").click()
    elif how == "Escape":
        app_page.keyboard.press("Escape")
    else:
        app_page.mouse.click(10, 450)
    evidence.expect(dialog, f"{how} closes the modal").to_be_hidden()
    evidence.expect(overlay(app_page), "the board is undimmed").to_be_hidden()
    evidence.expect(app_page, "back on the board's address").to_have_url(re.compile(r"/board$"))
    evidence.shot(
        "focus returns to the card that opened it",
        app_page.locator(f"[data-item={KEY}]"),
        held=focused_key(app_page) == KEY,
    )
    assert focused_key(app_page) == KEY, focused_key(app_page)


def test_reloading_at_the_cards_address_reopens_the_modal(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    open_card(app_page, server)
    app_page.reload()
    dialog = modal(app_page)
    evidence.expect(dialog, "the reload reopens the modal").to_be_visible()
    evidence.expect(dialog.get_by_role("heading", name=TITLE), "with the same title").to_be_visible()


def test_the_modal_is_at_most_760px_wide_and_long_text_wraps(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    change(own_server, description="x" * 600)
    dialog = open_card(app_page, own_server)
    geometry = dialog.evaluate(
        "m => ({ width: m.getBoundingClientRect().width, sideways: m.scrollWidth > m.clientWidth + 1 })"
    )
    evidence.shot(
        f"the card is at most 760px wide ({round(geometry['width'])}px)",
        dialog,
        held=geometry["width"] <= 761,
    )
    assert geometry["width"] <= 761, geometry
    evidence.shot("a 600-character word wraps: no sideways scroll", dialog, held=not geometry["sideways"])
    assert not geometry["sideways"], geometry


@pytest.mark.parametrize("size", [{"width": 900, "height": 700}, {"width": 375, "height": 812}])
def test_the_modal_fills_the_screen_below_1024px(
    app_page: Page, server: Server, evidence: Evidence, size: dict[str, int]
) -> None:
    app_page.set_viewport_size(size)
    app_page.goto(f"{server.url}/board/{KEY}")
    dialog = modal(app_page)
    dialog.wait_for(state="visible")
    g = dialog.evaluate(
        "m => { const r = m.getBoundingClientRect(); "
        "return { position: getComputedStyle(m).position, top: r.top, left: r.left, width: r.width, "
        "height: r.height }; }"
    )
    fills = (
        g["position"] == "fixed"
        and g["top"] <= 1
        and g["left"] <= 1
        and g["width"] >= size["width"] - 1
        and g["height"] >= size["height"] - 1
    )
    evidence.shot(f"at {size['width']}px the modal fills the screen", dialog, held=fills)
    assert fills, g


def test_create_opens_in_the_same_dimmed_modal(app_page: Page, server: Server, evidence: Evidence) -> None:
    app_page.set_viewport_size(DESKTOP)
    app_page.goto(f"{server.url}/board")
    app_page.get_by_role("button", name="Create").click()
    dialog = modal(app_page)
    evidence.expect(dialog, "Create opens in a modal").to_be_visible()
    evidence.expect(overlay(app_page), "over the dimmed board").to_be_visible()
    tiles = dialog.locator("[data-type]")
    evidence.expect(tiles, "four type tiles").to_have_count(4)
    evidence.expect(
        dialog.get_by_role("button", name="Create story"), "the button names the type"
    ).to_be_visible()
    app_page.keyboard.press("Escape")
    evidence.expect(dialog, "Escape closes it").to_be_hidden()


def test_create_makes_an_item_on_the_board(app_page: Page, own_server: Server, evidence: Evidence) -> None:
    app_page.set_viewport_size(DESKTOP)
    app_page.goto(f"{own_server.url}/board")
    app_page.get_by_role("button", name="Create").click()
    dialog = modal(app_page)
    dialog.get_by_role("button", name="Epic", exact=True).click()
    evidence.expect(
        dialog.get_by_role("button", name="Create epic"), "the button follows the type"
    ).to_be_visible()
    dialog.get_by_label("Title", exact=True).fill("A fresh epic")
    dialog.get_by_role("button", name="Create epic").click()
    evidence.expect(dialog, "the modal closes").to_be_hidden()
    card = app_page.get_by_role("region", name="To do").locator("[data-item]", has_text="A fresh epic")
    evidence.expect(card, "the epic is in To do").to_be_visible()


def test_add_child_opens_create_with_the_parent_set(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, server)
    dialog.get_by_role("button", name="Add child").click()
    evidence.expect(
        dialog.get_by_role("button", name="Create subtask"), "Create opens for a subtask"
    ).to_be_visible()
    evidence.expect(overlay(app_page), "over the dimmed board").to_be_visible()
    evidence.expect(dialog.get_by_label("Parent", exact=True), "the parent is this card").to_have_value(KEY)
    evidence.expect(
        dialog.get_by_label("Repository", exact=True), "and the repository is its parent's"
    ).to_have_value("weyenk/airlie")


def test_a_new_child_takes_the_parents_repository(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, own_server)
    dialog.get_by_role("button", name="Add child").click()
    dialog.get_by_label("Title", exact=True).fill("A child task")
    dialog.get_by_role("button", name="Create subtask").click()
    evidence.expect(dialog.get_by_role("heading", name=TITLE), "back on the parent card").to_be_visible()
    evidence.expect(dialog.get_by_text("A child task"), "the child is listed under it").to_be_visible()
    store = board(own_server)
    try:
        children = store.list(parent=KEY)
    finally:
        store.close()
    labels = [list(c.labels) for c in children]
    evidence.shot("the child carries the parent's repository label", held=labels == [["repo:weyenk/airlie"]])
    assert labels == [["repo:weyenk/airlie"]], labels


def test_tab_stays_inside_the_modal(app_page: Page, server: Server, evidence: Evidence) -> None:
    dialog = open_card(app_page, server)
    inside = True
    for _ in range(25):
        app_page.keyboard.press("Tab")
        inside = bool(dialog.evaluate("m => m.contains(document.activeElement)"))
        if not inside:
            break
    evidence.shot("repeated Tab stays inside the modal", dialog, held=inside)
    assert inside


def test_acceptance_criteria_colour_given_when_and_then(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, server)
    section = dialog.locator("[data-acceptance]")
    evidence.expect(section, "the story's criteria are shown").to_contain_text("Acceptance criteria")
    colours = [
        section.locator("[data-kw]").nth(i).evaluate("e => getComputedStyle(e).color") for i in range(3)
    ]
    evidence.shot("Given, when and then each have a colour", section, held=len(set(colours)) == 3)
    assert len(set(colours)) == 3, colours
    evidence.expect(dialog.locator("[data-workflow]"), "the workflow lists its layers").to_contain_text(
        "Layer 01"
    )
    evidence.expect(
        dialog.locator("[data-workflow]").get_by_role("link", name="Open pipeline"),
        "and links to the pipeline",
    ).to_have_attribute("href", f"/workflows/{KEY}")


def test_clicking_the_description_opens_a_prefilled_editor(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    change(own_server, description="The panel must be wide")
    dialog = open_card(app_page, own_server)
    rendered = dialog.get_by_text("The panel must be wide")
    rendered.click()
    editor = dialog.get_by_label("Description", exact=True)
    evidence.expect(editor, "an editor appears in the description's spot").to_be_visible()
    evidence.expect(editor, "pre-filled with the current description").to_have_value("The panel must be wide")
    evidence.expect(rendered, "the rendered text is hidden while editing").to_be_hidden()


def test_saving_the_description_patches_and_survives_a_reload(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    change(own_server, description="The panel must be wide")
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("The panel must be wide").click()
    dialog.get_by_label("Description", exact=True).fill("The panel must be wide and centred")
    dialog.get_by_role("button", name="Save").click()
    evidence.expect(dialog, "the card shows the new text").to_contain_text("wide and centred")
    app_page.reload()
    evidence.expect(modal(app_page), "after a reload it is still there").to_contain_text("wide and centred")
    assert stored(own_server).description == "The panel must be wide and centred"


def test_escape_in_an_editor_discards_it_and_keeps_the_modal(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    change(own_server, description="The panel must be wide")
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("The panel must be wide").click()
    editor = dialog.get_by_label("Description", exact=True)
    editor.fill("changed, then discarded")
    editor.press("Escape")
    evidence.expect(dialog, "the modal stays open").to_be_visible()
    evidence.expect(editor, "the editor is gone").to_be_hidden()
    evidence.expect(dialog.get_by_text("The panel must be wide"), "the old text is back").to_be_visible()
    assert stored(own_server).description == "The panel must be wide"
    app_page.keyboard.press("Escape")
    evidence.expect(dialog, "a second Escape, with no editor, closes it").to_be_hidden()


def test_a_link_in_the_description_opens_in_a_new_tab_without_an_editor(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    change(own_server, description="See [the docs](https://example.com/docs) for details")
    dialog = open_card(app_page, own_server)
    with app_page.expect_popup() as popup:
        dialog.get_by_role("link", name="the docs").click()
    evidence.shot("the link opens a new tab", held="example.com" in popup.value.url)
    assert "example.com" in popup.value.url
    evidence.expect(dialog.get_by_label("Description", exact=True), "no editor opens").to_have_count(0)


def test_the_empty_description_prompt_opens_an_empty_editor(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    change(own_server, description="")
    dialog = open_card(app_page, own_server)
    prompt = dialog.get_by_text("Add a more detailed description…")
    evidence.expect(prompt, "the prompt is shown").to_be_visible()
    prompt.click()
    evidence.expect(dialog.get_by_label("Description", exact=True), "an empty editor opens").to_have_value("")


def test_the_empty_repository_prompt_adds_a_repo_label(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    change(own_server, labels=["web"])
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("Add a repository").click()
    editor = dialog.get_by_label("Repository", exact=True)
    evidence.expect(editor, "the field opens empty").to_have_value("")
    editor.fill("weyenk/OpenHand")
    editor.press("Enter")
    evidence.expect(dialog, "the card names the repository").to_contain_text("weyenk/OpenHand")
    labels = list(stored(own_server).labels)
    evidence.shot("the label was stored, the others kept", held=labels == ["repo:weyenk/OpenHand", "web"])
    assert labels == ["repo:weyenk/OpenHand", "web"], labels


def test_clicking_the_repository_edits_it_and_saves_the_label(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("weyenk/airlie").click()
    editor = dialog.get_by_label("Repository", exact=True)
    evidence.expect(editor, "pre-filled with the current repository").to_have_value("weyenk/airlie")
    editor.fill("weyenk/OpenHand")
    editor.press("Enter")
    evidence.expect(dialog, "the card names the new repository").to_contain_text("weyenk/OpenHand")
    assert "repo:weyenk/OpenHand" in stored(own_server).labels


def test_a_repository_that_is_not_owner_slash_name_is_refused(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("weyenk/airlie").click()
    editor = dialog.get_by_label("Repository", exact=True)
    editor.fill("not-a-repo")
    editor.press("Enter")
    evidence.expect(dialog.locator("[data-repo]").get_by_role("alert"), "it says why").to_contain_text(
        "owner/name"
    )
    evidence.expect(editor, "the editor stays open with what was typed").to_have_value("not-a-repo")
    assert list(stored(own_server).labels) == ["repo:weyenk/airlie"]
    editor.press("Escape")
    evidence.expect(dialog, "Escape discards it; the old repository shows").to_contain_text("weyenk/airlie")


def test_a_full_known_repository_name_saves_on_one_enter(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    store = board(own_server)
    try:
        store.update("AIR-3", labels=["repo:weyenk/OpenHand"])
    finally:
        store.close()
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("weyenk/airlie").click()
    editor = dialog.get_by_label("Repository", exact=True)
    editor.fill("weyenk/OpenHand")
    evidence.expect(dialog.get_by_role("option").first, "the typed name is offered").to_be_visible()
    editor.press("Enter")
    evidence.expect(editor, "one Enter saved it and closed the field").to_have_count(0)
    evidence.expect(dialog, "the card names it").to_contain_text("weyenk/OpenHand")
    assert list(stored(own_server).labels) == ["repo:weyenk/OpenHand"]
    card = app_page.locator("[data-item=AIR-4]")
    evidence.expect(card, "the board's card carries the label").to_contain_text("repo:weyenk/OpenHand")


def test_focus_goes_to_create_and_back_into_the_card(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, server)
    dialog.get_by_role("button", name="Add child").click()
    title = dialog.get_by_label("Title", exact=True)
    evidence.expect(title, "Create's Title has focus").to_be_focused()
    dialog.get_by_role("button", name="Cancel").click()
    evidence.expect(dialog.get_by_role("heading", name=TITLE), "back on the card").to_be_visible()
    inside = dialog.evaluate("m => m.contains(document.activeElement)")
    evidence.shot("focus is inside the card modal", dialog, held=inside)
    assert inside


def test_closing_by_the_backdrop_saves_an_open_editor(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    change(own_server, description="The panel must be wide")
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("The panel must be wide").click()
    dialog.get_by_label("Description", exact=True).fill("Typed, then the backdrop")
    app_page.mouse.click(10, 450)
    evidence.expect(dialog, "the card closed").to_be_hidden()
    for _ in range(20):
        if stored(own_server).description == "Typed, then the backdrop":
            break
        app_page.wait_for_timeout(100)
    assert stored(own_server).description == "Typed, then the backdrop"


def test_back_after_closing_does_not_reopen_the_card(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, server)
    app_page.keyboard.press("Escape")
    evidence.expect(dialog, "closed").to_be_hidden()
    app_page.go_back()
    evidence.expect(modal(app_page), "Back does not bring the card back").to_have_count(0)


def test_a_child_given_another_repository_in_create_keeps_it_and_returns_to_the_parent(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, own_server)
    dialog.get_by_role("button", name="Add child").click()
    dialog.get_by_label("Title", exact=True).fill("Elsewhere")
    dialog.get_by_label("Repository", exact=True).fill("weyenk/OpenHand")
    dialog.get_by_role("button", name="Create subtask").click()
    evidence.expect(dialog.get_by_role("heading", name=TITLE), "back on the parent card").to_be_visible()
    evidence.expect(dialog.get_by_text("Elsewhere"), "the child is listed").to_be_visible()
    store = board(own_server)
    try:
        labels = [list(c.labels) for c in store.list(parent=KEY)]
    finally:
        store.close()
    evidence.shot("the child kept the repository typed in Create", held=labels == [["repo:weyenk/OpenHand"]])
    assert labels == [["repo:weyenk/OpenHand"]], labels


def test_the_repository_field_completes_a_known_repository_with_tab(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("weyenk/airlie").click()
    editor = dialog.get_by_label("Repository", exact=True)
    editor.fill("weyenk/air")
    evidence.expect(dialog.get_by_role("option").first, "the top match is offered").to_contain_text(
        "weyenk/airlie"
    )
    editor.press("Tab")
    evidence.expect(editor, "Tab completes the field").to_have_value("weyenk/airlie")
    editor.fill("weyenk/air")
    editor.press("Tab")
    editor.press("Enter")
    evidence.expect(
        dialog.get_by_label("Repository", exact=True), "Enter saved it and closed the field"
    ).to_have_count(0)
    evidence.expect(dialog, "the card names the repository").to_contain_text("weyenk/airlie")
    assert list(stored(own_server).labels) == ["repo:weyenk/airlie"]


def test_escape_closes_the_suggestions_first_then_discards_the_edit(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("weyenk/airlie").click()
    editor = dialog.get_by_label("Repository", exact=True)
    editor.fill("weyenk/air")
    evidence.expect(dialog.get_by_role("option").first, "the list is open").to_be_visible()
    editor.press("Escape")
    evidence.expect(dialog.get_by_role("option"), "the first Escape closes the list").to_have_count(0)
    evidence.expect(editor, "the editor is still open").to_be_visible()
    evidence.expect(dialog, "and so is the modal").to_be_visible()
    editor.press("Escape")
    evidence.expect(editor, "the second discards the edit").to_be_hidden()
    evidence.expect(dialog, "the modal still stays").to_be_visible()
    assert list(stored(own_server).labels) == ["repo:weyenk/airlie"]


def test_clicking_the_title_edits_it_and_enter_saves(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, own_server)
    dialog.get_by_role("heading", name=TITLE).click()
    editor = dialog.get_by_label("Title", exact=True)
    evidence.expect(editor, "pre-filled with the title").to_have_value(TITLE)
    editor.fill("Board: centred card modal")
    editor.press("Enter")
    evidence.expect(dialog, "the card shows the new title").to_contain_text("Board: centred card modal")
    assert stored(own_server).title == "Board: centred card modal"


def test_clicking_a_different_section_saves_the_first_and_opens_the_second(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    change(own_server, description="The panel must be wide")
    dialog = open_card(app_page, own_server)
    dialog.get_by_text("The panel must be wide").click()
    dialog.get_by_label("Description", exact=True).fill("The panel must be wide and centred")
    dialog.get_by_role("heading", name=TITLE).click()
    evidence.expect(dialog.get_by_label("Title", exact=True), "the clicked section opens").to_be_visible()
    evidence.expect(dialog.get_by_label("Description", exact=True), "the first one closed").to_have_count(0)
    app_page.wait_for_function("() => document.body.innerText.includes('wide and centred')")
    assert stored(own_server).description == "The panel must be wide and centred"


def test_editing_your_own_comment_saves_it_and_someone_elses_stays_read_only(
    app_page: Page, own_server: Server, evidence: Evidence
) -> None:
    store = board(own_server)
    try:
        store.comment(KEY, "api-key", "first note")
        store.comment(KEY, "noah", "a note from noah")
    finally:
        store.close()
    dialog = open_card(app_page, own_server)
    mine = dialog.locator("[data-comment]").nth(0)
    theirs = dialog.locator("[data-comment]").nth(1)
    evidence.expect(
        theirs.get_by_role("button", name="Edit comment by noah"), "no Edit on someone else's"
    ).to_have_count(0)
    theirs.get_by_text("a note from noah").click()
    evidence.expect(theirs.get_by_role("textbox"), "clicking it opens nothing").to_have_count(0)
    mine.get_by_role("button", name="Edit comment by api-key").click()
    editor = mine.get_by_role("textbox")
    evidence.expect(editor, "your own opens pre-filled").to_have_value("first note")
    editor.fill("first note, edited")
    mine.get_by_role("button", name="Save").click()
    evidence.expect(dialog, "the comment shows the new text").to_contain_text("first note, edited")
    texts = [c.text for c in stored(own_server).comments]
    assert "first note, edited" in texts, texts


def test_a_new_comment_is_added(app_page: Page, own_server: Server, evidence: Evidence) -> None:
    dialog = open_card(app_page, own_server)
    dialog.get_by_label("Add a comment", exact=True).fill("Looks right")
    dialog.get_by_role("button", name="Comment", exact=True).click()
    evidence.expect(
        dialog.locator("[data-comment]", has_text="Looks right"), "the comment is listed"
    ).to_be_visible()
    assert [c.text for c in stored(own_server).comments] == ["Looks right"]


def test_on_the_phone_criteria_and_workflow_start_folded(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size({"width": 375, "height": 812})
    app_page.goto(f"{server.url}/board/{KEY}")
    dialog = modal(app_page)
    acceptance = dialog.locator("[data-acceptance]")
    workflow = dialog.locator("[data-workflow]")
    evidence.expect(acceptance, "Acceptance criteria is there").to_be_visible()
    evidence.expect(workflow, "Workflow is there").to_be_visible()
    evidence.expect(acceptance.get_by_text("an open panel"), "its criteria are folded").to_be_hidden()
    evidence.expect(workflow.get_by_text("Layer 01"), "its layers are folded").to_be_hidden()
    acceptance.get_by_role("button", name="Acceptance criteria").click()
    workflow.get_by_role("button", name="Workflow").click()
    evidence.expect(acceptance.get_by_text("an open panel").first, "a tap opens the criteria").to_be_visible()
    evidence.expect(workflow.get_by_text("Layer 01"), "a tap opens the workflow").to_be_visible()


def test_a_card_with_no_story_has_no_criteria_or_workflow_section(
    app_page: Page, server: Server, evidence: Evidence
) -> None:
    dialog = open_card(app_page, server, "AIR-3")
    evidence.expect(
        dialog.get_by_role("heading", name="Board: columns fill the page width"), "open"
    ).to_be_visible()
    evidence.expect(dialog.locator("[data-acceptance]"), "no criteria section").to_have_count(0)
    evidence.expect(dialog.locator("[data-workflow]"), "no workflow section").to_have_count(0)
