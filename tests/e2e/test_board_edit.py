"""The Board's card modal in Chromium: in-place editing of every text section. Clicking a section hides its
rendered text and shows an editor pre-filled with the current value plus Save and Cancel; Save issues the
PATCH, Cancel and Escape discard, Enter or clicking away saves, and at most one section is ever open. Each
check leaves its screenshot in test-results/evidence/ (conftest.Evidence).

The board is the shared CARDS from conftest: AIR-1..AIR-7, so "Board: panel width" is AIR-4 (Approved)."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page

from airlie.board.store import BoardStore
from tests.e2e.conftest import Evidence, Server

pytestmark = pytest.mark.e2e

TITLE = "Board: panel width"
KEY = "AIR-4"


def _modal(page: Page) -> Any:
    return page.locator("[role='dialog'][aria-modal='true']")


def _board(server: Server) -> BoardStore:
    return BoardStore(server.state / "board.sqlite")


def _editor_with_value(page: Page, value: str) -> Any:
    """The opened section editor whose current value is `value`, whatever element the coder uses (input or
    textarea): it is marked in the DOM and returned as a locator, so it works for both."""
    page.evaluate(
        """(v) => {
            const els = [...document.querySelectorAll(
                '[role="dialog"][aria-modal="true"] input, [role="dialog"][aria-modal="true"] textarea')];
            for (const e of els) { if (e.value === v) e.dataset.e2eEditor = "1"; }
        }""",
        value,
    )
    return page.locator("[data-e2e-editor='1']")


def open_modal(page: Page, url: str, title: str = TITLE) -> None:
    """Sign in at desktop width, land on the board, and open the modal for the named card."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{url}/#/board")
    page.locator("section.column").nth(5).wait_for()
    page.locator(".item", has_text=title).click()
    _modal(page).wait_for(state="visible")


def test_clicking_the_description_opens_a_prefilled_editor(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC4: with a non-empty markdown description, clicking the rendered text turns that spot into an editor
    pre-filled with the current description plus Save and Cancel, and the rendered text is hidden while
    editing."""
    board = _board(own_server)
    try:
        board.update(KEY, description="The panel must be wide")
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    rendered = modal.get_by_text("The panel must be wide").last

    rendered.click()

    editor = modal.get_by_label("Description")
    evidence.expect(editor, "an editor appears in the description's spot").to_be_visible()
    evidence.expect(editor, "the editor is pre-filled with the current description").to_have_value(
        "The panel must be wide"
    )
    evidence.expect(modal.get_by_role("button", name="Save"), "a Save button appears").to_be_visible()
    evidence.expect(modal.get_by_role("button", name="Cancel"), "a Cancel button appears").to_be_visible()
    evidence.expect(rendered, "the rendered text is hidden while editing").to_be_hidden()


def test_saving_the_description_patches_and_survives_a_reload(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC5: changing the description and clicking Save sends it to PATCH /v1/board/items/{key}, the card shows
    the updated rendered markdown, and after a reload the description still shows the updated text."""
    board = _board(own_server)
    try:
        board.update(KEY, description="The panel must be wide")
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    rendered = modal.get_by_text("The panel must be wide").last

    rendered.click()
    editor = modal.get_by_label("Description")
    editor.fill("The panel must be wide and centered")
    modal.get_by_role("button", name="Save").click()

    evidence.expect(modal, "the card shows the updated rendered markdown").to_contain_text(
        "The panel must be wide and centered"
    )
    evidence.expect(
        modal.get_by_text("The panel must be wide", exact=True).last, "the old text is gone"
    ).to_be_hidden()

    signed_in.reload()
    signed_in.locator("section.column").nth(5).wait_for()
    modal = _modal(signed_in)
    evidence.expect(modal, "after a reload the description still shows the updated text").to_contain_text(
        "The panel must be wide and centered"
    )
    board = _board(own_server)
    try:
        stored = board.get(KEY).description
    finally:
        board.close()
    assert stored == "The panel must be wide and centered", stored


def test_escape_in_the_description_editor_discards_and_keeps_the_modal(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC6: with the description editor open and changed, pressing Escape discards the edit, the rendered text
    reverts to its previous value, and the modal stays open."""
    board = _board(own_server)
    try:
        board.update(KEY, description="The panel must be wide")
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    rendered = modal.get_by_text("The panel must be wide").last

    rendered.click()
    editor = modal.get_by_label("Description")
    editor.fill("changed, then discarded")
    editor.press("Escape")

    evidence.expect(modal, "the modal stays open").to_be_visible()
    evidence.expect(editor, "the editor is gone").to_be_hidden()
    evidence.expect(rendered, "the rendered text reverts to its previous value").to_contain_text(
        "The panel must be wide"
    )
    board = _board(own_server)
    try:
        stored = board.get(KEY).description
    finally:
        board.close()
    assert stored == "The panel must be wide", stored


def test_clicking_a_link_in_the_description_opens_it_without_an_editor(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC7: with a description containing a markdown link, clicking the link follows it in a new tab and no
    editor opens."""
    board = _board(own_server)
    try:
        board.update(KEY, description="See [the docs](https://example.com/docs) for details")
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)

    with signed_in.expect_popup() as popup_info:
        modal.get_by_role("link", name="the docs").click()

    popup = popup_info.value
    evidence.shot("the link is followed in a new tab", held="example.com" in popup.url)
    assert "example.com" in popup.url, popup.url
    evidence.expect(modal.get_by_role("button", name="Save"), "no editor opens").to_have_count(0)
    evidence.expect(modal.get_by_role("button", name="Cancel"), "no editor opens").to_have_count(0)
    evidence.expect(modal, "the modal stays open with the rendered link").to_contain_text("the docs")


def test_clicking_the_empty_description_prompt_opens_an_empty_editor(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC8: with an empty description, clicking the clickable prompt 'Add a more detailed description…' opens
    an empty description editor in that spot."""
    board = _board(own_server)
    try:
        board.update(KEY, description="")
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    prompt = modal.get_by_text("Add a more detailed description…").last

    evidence.expect(prompt, "the empty-description prompt is shown").to_be_visible()
    prompt.click()

    editor = modal.get_by_label("Description")
    evidence.expect(editor, "an empty description editor opens in that spot").to_be_visible()
    evidence.expect(editor, "the editor starts empty").to_have_value("")
    evidence.expect(modal.get_by_role("button", name="Save"), "a Save button appears").to_be_visible()
    evidence.expect(modal.get_by_role("button", name="Cancel"), "a Cancel button appears").to_be_visible()


def test_clicking_the_empty_repository_prompt_opens_an_input_and_saving_adds_a_repo_label(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC9: with an empty repository, clicking the clickable prompt 'Add a repository' opens a repository
    input pre-filled empty, and saving a value adds a repo:<owner>/<name> label."""
    board = _board(own_server)
    try:
        board.update(KEY, labels=["web"])  # drop the repo label so the repository is empty
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    prompt = modal.get_by_text("Add a repository").last

    evidence.expect(prompt, "the empty-repository prompt is shown").to_be_visible()
    prompt.click()

    editor = modal.get_by_label("Repository")
    evidence.expect(editor, "a repository input opens in place").to_be_visible()
    evidence.expect(editor, "the input is pre-filled empty").to_have_value("")

    editor.fill("weyenk/OpenHand")
    modal.get_by_role("button", name="Save").click()

    evidence.expect(modal, "the modal names the new repository").to_contain_text("weyenk/OpenHand")
    board = _board(own_server)
    try:
        labels = board.get(KEY).labels
    finally:
        board.close()
    assert "repo:weyenk/OpenHand" in labels, labels


def test_clicking_the_repository_opens_a_prefilled_input_and_saving_updates_the_label(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC10: with a repository set, clicking the displayed repository text turns it into an input pre-filled
    with the current owner/name, and saving updates the repo:<owner>/<name> label."""
    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    repo_text = modal.get_by_text("weyenk/airlie").last

    evidence.expect(repo_text, "the repository is displayed").to_be_visible()
    repo_text.click()

    editor = modal.get_by_label("Repository")
    evidence.expect(editor, "the repository turns into an input").to_be_visible()
    evidence.expect(editor, "the input is pre-filled with the current owner/name").to_have_value(
        "weyenk/airlie"
    )

    editor.fill("weyenk/OpenHand")
    modal.get_by_role("button", name="Save").click()

    evidence.expect(modal, "the modal names the new repository").to_contain_text("weyenk/OpenHand")
    board = _board(own_server)
    try:
        labels = board.get(KEY).labels
    finally:
        board.close()
    assert "repo:weyenk/OpenHand" in labels, labels


def test_clicking_the_labels_opens_a_prefilled_input_and_saving_updates_them(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC11: with labels other than the repository, clicking the displayed labels turns them into a
    comma-separated input pre-filled with the current non-repo labels, and saving updates the labels."""
    board = _board(own_server)
    try:
        board.update(KEY, labels=["repo:weyenk/airlie", "web", "board"])
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    labels_text = modal.get_by_text("web").last

    evidence.expect(labels_text, "the non-repo labels are displayed").to_be_visible()
    labels_text.click()

    editor = modal.get_by_label("Labels")
    evidence.expect(editor, "the labels turn into a comma-separated input").to_be_visible()
    evidence.expect(editor, "the input is pre-filled with the current non-repo labels").to_have_value(
        "web, board"
    )

    editor.fill("web, board, modal")
    modal.get_by_role("button", name="Save").click()

    board = _board(own_server)
    try:
        labels = board.get(KEY).labels
    finally:
        board.close()
    assert "modal" in labels and "repo:weyenk/airlie" in labels, labels


def test_clicking_the_title_opens_a_prefilled_input_and_enter_saves(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC12: with the title shown as text, clicking it turns it into an input pre-filled with the current
    title, and pressing Enter saves it."""
    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    title_text = modal.get_by_role("heading", name=TITLE).last

    evidence.expect(title_text, "the title is shown as text").to_be_visible()
    title_text.click()

    editor = modal.get_by_label("Title")
    evidence.expect(editor, "the title turns into an input").to_be_visible()
    evidence.expect(editor, "the input is pre-filled with the current title").to_have_value(TITLE)

    editor.fill("Board: centered card modal")
    editor.press("Enter")

    evidence.expect(modal, "the modal shows the new title").to_contain_text("Board: centered card modal")
    board = _board(own_server)
    try:
        stored = board.get(KEY).title
    finally:
        board.close()
    assert stored == "Board: centered card modal", stored


def test_clicking_a_different_section_saves_the_first_and_opens_the_second(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC13: with one section open for editing, clicking a different section saves the first and opens the
    second, so at most one section is ever open at a time."""
    board = _board(own_server)
    try:
        board.update(KEY, description="The panel must be wide")
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)

    # Open the description editor and change it.
    modal.get_by_text("The panel must be wide").last.click()
    desc_editor = modal.get_by_label("Description")
    desc_editor.fill("The panel must be wide and centered")

    # Click a different section (the title): the description editor must save and close.
    modal.get_by_role("heading", name=TITLE).last.click()

    evidence.expect(desc_editor, "the first section is closed").to_be_hidden()
    title_editor = modal.get_by_label("Title")
    evidence.expect(title_editor, "the newly clicked section opens for editing").to_be_visible()
    board = _board(own_server)
    try:
        stored = board.get(KEY).description
    finally:
        board.close()
    assert stored == "The panel must be wide and centered", stored


def test_clicking_own_comment_opens_a_prefilled_editor_and_saving_patches_it(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC14: with a comment written by the signed-in user, clicking it turns it into an editor pre-filled with
    the comment text, and saving sends PATCH /v1/board/items/{key}/comments/{id} with the new text."""
    board = _board(own_server)
    try:
        board.comment(KEY, "api-key", "first note")
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    comment_text = modal.get_by_text("first note").last

    evidence.expect(comment_text, "the comment is displayed").to_contain_text("first note")
    comment_text.click()

    # The comment editor is the field pre-filled with the comment text (the "Add a comment" box holds none).
    editor = _editor_with_value(signed_in, "first note")
    evidence.expect(editor, "the comment turns into an editor pre-filled with its text").to_have_count(1)
    evidence.expect(editor, "the editor is pre-filled with the comment text").to_have_value("first note")

    editor.fill("first note, edited")
    modal.get_by_role("button", name="Save").click()

    evidence.expect(modal, "the comment shows the new text").to_contain_text("first note, edited")
    board = _board(own_server)
    try:
        texts = [c.text for c in board.get(KEY).comments]
    finally:
        board.close()
    assert "first note, edited" in texts, texts


def test_clicking_someone_elses_comment_stays_read_only(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC15: with a comment written by someone other than the signed-in user, clicking it does not make it
    editable and it stays read-only."""
    board = _board(own_server)
    try:
        board.comment(KEY, "noah", "a note from noah")
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)
    comment_text = modal.get_by_text("a note from noah").last

    evidence.expect(comment_text, "the comment is displayed").to_contain_text("a note from noah")
    comment_text.click()

    evidence.expect(
        modal.get_by_role("button", name="Save"), "no editor opens for someone else's comment"
    ).to_have_count(0)
    evidence.expect(
        modal.get_by_role("button", name="Cancel"), "no editor opens for someone else's comment"
    ).to_have_count(0)
    evidence.expect(comment_text, "the comment stays read-only").to_contain_text("a note from noah")


def test_escape_cancels_only_the_open_editor_and_keeps_the_modal(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    """AC17: with the description editor open, pressing Escape cancels only that edit and the modal stays open
    with the other sections intact."""
    board = _board(own_server)
    try:
        board.update(KEY, description="The panel must be wide")
    finally:
        board.close()

    open_modal(signed_in, own_server.url)
    modal = _modal(signed_in)

    modal.get_by_text("The panel must be wide").last.click()
    editor = modal.get_by_label("Description")
    editor.fill("changed, then discarded")
    editor.press("Escape")

    evidence.expect(modal, "the modal stays open").to_be_visible()
    evidence.expect(editor, "the description editor is cancelled").to_be_hidden()
    evidence.expect(
        modal.get_by_text("The panel must be wide").last, "the description reverts"
    ).to_contain_text("The panel must be wide")
    evidence.expect(modal, "the other sections stay intact").to_contain_text("in Approved")
    evidence.expect(
        modal.get_by_role("button", name="Close"), "the modal's close button is still there"
    ).to_be_visible()
