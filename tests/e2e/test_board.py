"""The Board screen in Chromium: sign in, six status columns holding the board's cards, the item panel. Each
check leaves its screenshot in test-results/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

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


def test_a_card_opens_its_panel_and_close_puts_the_board_back(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    signed_in.goto(f"{server.url}/#/board")
    signed_in.locator(".item", has_text="Board: panel width").click()
    evidence.expect(signed_in, "the card's own address").to_have_url(re.compile(r"#/board/AIR-4$"))
    panel = signed_in.locator("aside.panel")
    evidence.expect(panel, "the item panel opens").to_be_visible()
    title = panel.get_by_role("heading", name="Board: panel width")
    evidence.expect(title, "the panel shows the card's title").to_be_visible()
    panel.get_by_role("button", name="Close").click()
    evidence.expect(panel, "Close hides the panel").to_be_hidden()
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
