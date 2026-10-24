"""The Board screen in Chromium: sign in, six status columns holding the board's cards, the item panel. Each
check leaves its screenshot in test-results/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

from airlie.board.store import BoardStore
from tests.e2e.conftest import KEY, Evidence, Server

pytestmark = pytest.mark.e2e

COLUMNS = ["To do", "Approved", "Doing", "Review", "Blocked", "Done"]


def test_sign_in_with_the_admin_key_lands_on_the_page_asked_for(
    page: Page, server: Server, evidence: Evidence
) -> None:
    page.goto(f"{server.url}/#/board")
    evidence.expect(page, "the board sends a stranger to sign in").to_have_url(re.compile(r"#/signin$"))
    page.get_by_label("Admin key").fill(KEY)
    page.get_by_role("button", name="Sign in with the key").click()
    evidence.expect(page, "the key goes back to the board").to_have_url(re.compile(r"#/board$"))
    evidence.expect(page.get_by_role("heading", name="Board"), "the board's heading").to_be_visible()


def test_a_wrong_key_stays_on_sign_in(page: Page, server: Server, evidence: Evidence) -> None:
    page.goto(f"{server.url}/#/signin")
    page.get_by_label("Admin key").fill("not-the-key")
    page.get_by_role("button", name="Sign in with the key").click()
    evidence.expect(page.get_by_role("alert"), "sign in says the key is wrong").not_to_be_empty()
    evidence.expect(page, "still on sign in").to_have_url(re.compile(r"#/signin$"))


def test_the_board_has_a_column_per_status_in_order(
    signed_in: Page, server: Server, evidence: Evidence, errors: list[str]
) -> None:
    signed_in.goto(f"{server.url}/#/board")
    columns = signed_in.locator("section.column")
    evidence.expect(columns, "one column per status").to_have_count(len(COLUMNS))
    labels = [c.get_attribute("aria-label") for c in columns.all()]
    evidence.shot("the columns in status order", columns, held=labels == COLUMNS)
    assert labels == COLUMNS
    evidence.shot("nothing logged an error", held=not errors)
    assert errors == []


@pytest.mark.parametrize(
    ("column", "title"),
    [
        ("To do", "Board: columns fill the page width"),
        ("Approved", "Board: panel width"),
        ("Doing", "Ship the web app"),
        ("Review", "Jira import keeps its keys"),
        ("Blocked", "Sign-in remembers the device"),
        ("Done", "Chat streams its answer"),
    ],
)
def test_each_card_sits_in_its_status_column(
    signed_in: Page, server: Server, evidence: Evidence, column: str, title: str
) -> None:
    signed_in.goto(f"{server.url}/#/board")
    card = signed_in.get_by_role("region", name=column).locator(".item", has_text=title)
    evidence.expect(card, f"'{title}' in {column}").to_be_visible()


def test_a_card_opens_its_modal_and_close_puts_the_board_back(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    signed_in.goto(f"{server.url}/#/board")
    signed_in.locator(".item", has_text="Board: panel width").click()
    evidence.expect(signed_in, "the card's own address").to_have_url(re.compile(r"#/board/AIR-4$"))
    modal = signed_in.locator("[role='dialog'][aria-modal='true']")
    evidence.expect(modal, "the card modal opens").to_be_visible()
    title = modal.get_by_role("heading", name="Board: panel width")
    evidence.expect(title, "the modal shows the card's title").to_be_visible()
    modal.get_by_role("button", name="Close").click()
    evidence.expect(modal, "Close hides the modal").to_be_hidden()
    evidence.expect(signed_in, "back on the board's address").to_have_url(re.compile(r"#/board$"))


def test_the_level_filter_shows_only_that_level(signed_in: Page, server: Server, evidence: Evidence) -> None:
    signed_in.goto(f"{server.url}/#/board")
    cards = signed_in.locator(".item")
    evidence.expect(cards, "every card, unfiltered").to_have_count(7)
    signed_in.get_by_role("group", name="Level").get_by_role("button", name="Epics").click()
    evidence.expect(cards, "Epics leaves one card").to_have_count(1)
    evidence.expect(cards, "the epic").to_contain_text("Board polish")


def test_the_board_loads_at_phone_width(
    signed_in: Page, server: Server, evidence: Evidence, errors: list[str]
) -> None:
    signed_in.set_viewport_size({"width": 375, "height": 812})
    signed_in.goto(f"{server.url}/#/board")
    evidence.expect(signed_in.locator("section.column"), "six columns at 375px").to_have_count(len(COLUMNS))
    evidence.expect(
        signed_in.get_by_role("region", name="To do"), "the first column on screen"
    ).to_be_visible()
    evidence.shot("nothing logged an error", held=not errors)
    assert errors == []


def _board(server: Server) -> BoardStore:
    return BoardStore(server.state / "board.sqlite")


def test_edit_sets_a_cards_repository(signed_in: Page, own_server: Server, evidence: Evidence) -> None:
    signed_in.goto(f"{own_server.url}/#/board/AIR-4")
    modal = signed_in.locator("[role='dialog'][aria-modal='true']")
    evidence.expect(modal, "the modal names the card's repository").to_contain_text(
        "Repository weyenk/airlie"
    )
    modal.get_by_text("Edit", exact=True).click()
    repo = modal.get_by_label("Repository")
    evidence.expect(repo, "Edit holds the repository as owner/name").to_have_value("weyenk/airlie")
    repo.fill("weyenk/OpenHand")
    modal.get_by_label("Labels").fill("web, board")
    modal.get_by_role("button", name="Save").click()
    evidence.expect(modal, "the modal names the new repository").to_contain_text("Repository weyenk/OpenHand")
    card = signed_in.locator(".item", has_text="Board: panel width")
    evidence.expect(card, "the card carries the new repo: label").to_contain_text("repo:weyenk/OpenHand")
    board = _board(own_server)
    try:
        labels = board.get("AIR-4").labels
    finally:
        board.close()
    evidence.shot(
        "the board stored one repo: label and the others",
        card,
        held=labels == ("repo:weyenk/OpenHand", "web", "board"),
    )
    assert labels == ("repo:weyenk/OpenHand", "web", "board")


def test_a_repository_that_is_not_owner_slash_name_is_not_saved(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    signed_in.goto(f"{own_server.url}/#/board/AIR-4")
    modal = signed_in.locator("[role='dialog'][aria-modal='true']")
    modal.get_by_text("Edit", exact=True).click()
    modal.get_by_label("Repository").fill("OpenHand")
    modal.get_by_role("button", name="Save").click()
    evidence.expect(modal, "the modal keeps the old repository").to_contain_text("Repository weyenk/airlie")
    board = _board(own_server)
    try:
        labels = board.get("AIR-4").labels
    finally:
        board.close()
    assert labels == ("repo:weyenk/airlie",)


def test_a_new_child_starts_with_its_parents_repository(
    signed_in: Page, own_server: Server, evidence: Evidence
) -> None:
    signed_in.goto(f"{own_server.url}/#/board/AIR-4")
    modal = signed_in.locator("[role='dialog'][aria-modal='true']")
    modal.get_by_role("button", name="Add child").click()
    repo = modal.get_by_label("Repository")
    evidence.expect(repo, "the new subtask has its story's repository").to_have_value("weyenk/airlie")
    modal.get_by_label("Title").fill("Panel width on phones")
    repo.fill("weyenk/OpenHand")
    modal.get_by_role("button", name="Create").click()
    evidence.expect(signed_in, "Create opens the new card").to_have_url(re.compile(r"#/board/AIR-8$"))
    evidence.expect(modal, "the new card names its repository").to_contain_text("Repository weyenk/OpenHand")
    board = _board(own_server)
    try:
        made = board.get("AIR-8")
    finally:
        board.close()
    assert (made.parent, made.labels) == ("AIR-4", ("repo:weyenk/OpenHand",))
