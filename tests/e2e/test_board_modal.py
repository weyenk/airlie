"""The Board's card modal in Chromium: a wide, centered, Trello-style card over the dimmed board replaces the
380px side panel. Each check leaves its screenshot in test-results/evidence/ (conftest.Evidence).

The board is the shared CARDS from conftest: AIR-1..AIR-7, so "Board: panel width" is AIR-4 (Approved) and
"Ship the web app" is AIR-1 (Doing)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import Evidence, Server

pytestmark = pytest.mark.e2e

# A long description with no spaces: it cannot wrap, so a card that does not wrap would overflow it.
LONG = "x" * 600

# The card modal's title is the card's own key and title, as the panel's was.
TITLE = "Board: panel width"
KEY = "AIR-4"


def _modal(page: Page) -> Any:
    """The card modal, whatever class the coder gives its card (the brief pins role/aria-modal, not a
    class)."""
    return page.locator("[role='dialog'][aria-modal='true']")


def _backdrop(page: Page) -> Any:
    """The dimmed backdrop behind the card modal."""
    return page.locator(".backdrop")


def open_modal(page: Page, server: Server) -> None:
    """Sign in at desktop width, land on the board, and open the modal for the Approved card."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{server.url}/#/board")
    page.locator("section.column").nth(5).wait_for()
    page.locator(".item", has_text=TITLE).click()
    _modal(page).wait_for(state="visible")


def test_clicking_a_card_opens_a_centered_modal_over_the_dimmed_board(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    """AC1: a card click opens a centered card modal over the dimmed board; the columns keep their full
    width (no panel column is reserved); the modal shows a large title with 'in <status>' directly
    under it."""
    open_modal(signed_in, server)

    modal = _modal(signed_in)
    evidence.expect(modal, "the card modal opens").to_be_visible()

    # The board is dimmed behind the modal.
    backdrop = _backdrop(signed_in)
    evidence.expect(backdrop, "the board is dimmed behind the modal").to_be_visible()

    # The old 380px side panel is gone (AC22): the centered modal replaces it.
    evidence.expect(signed_in.locator(".panel"), "the old .panel aside is gone").to_have_count(0)

    # The columns keep their full width: no panel column is reserved.
    grid = signed_in.locator(".columns")
    widths = signed_in.evaluate(
        """() => {
            const g = document.querySelector(".columns");
            const cols = [...document.querySelectorAll("section.column")].map(
                (c) => c.getBoundingClientRect().width);
            return { grid: g.clientWidth, cols };
        }"""
    )
    # With no panel reserving 380px, the grid uses the full page width.
    evidence.shot(
        "the columns keep their full width (no panel reserved)",
        grid,
        held=widths["grid"] >= 1280 - 40,  # the .page's max-width minus its padding
    )
    assert widths["grid"] >= 1280 - 40, widths

    # The modal shows a large title with 'in <status>' directly under it.
    evidence.expect(modal, "the modal shows the card's title").to_contain_text(TITLE)
    evidence.expect(modal, "the modal shows 'in <status>' under the title").to_contain_text("in Approved")


def test_the_modal_is_centered_over_the_board(signed_in: Page, server: Server, evidence: Evidence) -> None:
    """AC1: the card modal is centered over the board."""
    open_modal(signed_in, server)
    modal = _modal(signed_in)
    box = modal.bounding_box()
    assert box is not None
    centered = abs((box["x"] + box["width"] / 2) - 640) < 40  # viewport center at 1280px
    evidence.shot("the modal is centered on the page", modal, held=centered)
    assert centered, box


def test_closing_with_the_x_button_restores_the_board(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    """AC2: clicking the X button closes the modal, undims the board, returns focus to the card, and sets
    the hash back to #/board."""
    open_modal(signed_in, server)
    modal = _modal(signed_in)
    card = signed_in.locator(".item", has_text=TITLE)

    modal.get_by_role("button", name="Close").click()

    evidence.expect(modal, "the X button closes the modal").to_be_hidden()
    evidence.expect(_backdrop(signed_in), "the board is undimmed").to_be_hidden()
    evidence.expect(signed_in, "back on the board's address").to_have_url(re.compile(r"#/board$"))
    focused = signed_in.evaluate("() => document.activeElement?.dataset?.key ?? ''")
    evidence.shot("focus returns to the card that opened it", card, held=focused == KEY)
    assert focused == KEY, focused


def test_closing_with_escape_restores_the_board(signed_in: Page, server: Server, evidence: Evidence) -> None:
    """AC2: pressing Escape while no editor is open closes the modal, undims the board, returns focus to the
    card, and sets the hash back to #/board."""
    open_modal(signed_in, server)
    modal = _modal(signed_in)
    card = signed_in.locator(".item", has_text=TITLE)

    signed_in.keyboard.press("Escape")

    evidence.expect(modal, "Escape closes the modal").to_be_hidden()
    evidence.expect(_backdrop(signed_in), "the board is undimmed").to_be_hidden()
    evidence.expect(signed_in, "back on the board's address").to_have_url(re.compile(r"#/board$"))
    focused = signed_in.evaluate("() => document.activeElement?.dataset?.key ?? ''")
    evidence.shot("focus returns to the card that opened it", card, held=focused == KEY)
    assert focused == KEY, focused


def test_closing_by_clicking_the_backdrop_restores_the_board(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    """AC2: clicking the dimmed backdrop (not the card) closes the modal, undims the board, returns focus to
    the card, and sets the hash back to #/board."""
    open_modal(signed_in, server)
    modal = _modal(signed_in)
    card = signed_in.locator(".item", has_text=TITLE)

    # Click the backdrop, not the card: a point on the backdrop that is outside the centered card.
    signed_in.mouse.click(10, 450)

    evidence.expect(modal, "clicking the backdrop closes the modal").to_be_hidden()
    evidence.expect(_backdrop(signed_in), "the board is undimmed").to_be_hidden()
    evidence.expect(signed_in, "back on the board's address").to_have_url(re.compile(r"#/board$"))
    focused = signed_in.evaluate("() => document.activeElement?.dataset?.key ?? ''")
    evidence.shot("focus returns to the card that opened it", card, held=focused == KEY)
    assert focused == KEY, focused


def test_reloading_at_the_cards_address_reopens_the_modal(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    """AC3: reloading at #/board/AIR-4 reopens the modal for that card, with the same title and
    'in <status>' line as before the reload."""
    open_modal(signed_in, server)
    modal = _modal(signed_in)
    evidence.expect(modal, "the modal is open before the reload").to_be_visible()
    evidence.expect(modal, "the title before the reload").to_contain_text(TITLE)
    evidence.expect(modal, "'in <status>' before the reload").to_contain_text("in Approved")

    signed_in.reload()
    signed_in.locator("section.column").nth(5).wait_for()

    modal = _modal(signed_in)
    evidence.expect(modal, "the reload reopens the modal").to_be_visible()
    evidence.expect(modal, "the same title after the reload").to_contain_text(TITLE)
    evidence.expect(modal, "the same 'in <status>' line after the reload").to_contain_text("in Approved")


def test_the_modal_is_at_most_768px_wide_and_wraps_long_text(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    """AC18: at desktop width the card is at most 768px wide, long text wraps so no horizontal scrollbar
    appears on the card, and the card scrolls with the page rather than having its own inner scrollbar."""
    open_modal(signed_in, server)
    modal = _modal(signed_in)

    # Give the card a long, unbreakable description so it would overflow if it did not wrap.
    signed_in.evaluate(
        """(text) => {
            const modal = document.querySelector("[role='dialog'][aria-modal='true']");
            if (!modal) return;
            let desc = modal.querySelector(".desc");
            if (!desc) {
                desc = document.createElement("div");
                desc.className = "desc";
                modal.append(desc);
            }
            desc.textContent = text;
        }""",
        LONG,
    )

    g = signed_in.evaluate(
        """() => {
            const modal = document.querySelector("[role='dialog'][aria-modal='true']");
            if (!modal) return null;
            const style = getComputedStyle(modal);
            const r = modal.getBoundingClientRect();
            return {
                width: r.width,
                maxWidth: style.maxWidth,
                overflowX: style.overflowX,
                innerScroll: modal.scrollWidth > modal.clientWidth + 1,
            };
        }"""
    )
    assert g is not None
    under = g["width"] <= 768 + 1
    evidence.shot(f"the card is at most 768px wide ({round(g['width'])}px)", modal, held=under)
    assert under, g
    wraps = not g["innerScroll"] and g["overflowX"] in ("visible", "auto")
    evidence.shot("long text wraps: no horizontal scrollbar on the card", modal, held=wraps)
    assert wraps, g


def test_the_modal_fills_the_screen_below_1024px(signed_in: Page, server: Server, evidence: Evidence) -> None:
    """AC19: below 1024px the modal fills the whole screen (position fixed, inset 0)."""
    signed_in.set_viewport_size({"width": 375, "height": 812})
    signed_in.goto(f"{server.url}/#/board")
    signed_in.locator("section.column").nth(5).wait_for()
    signed_in.locator(".item", has_text=TITLE).click()
    modal = _modal(signed_in)
    modal.wait_for(state="visible")

    g = signed_in.evaluate(
        """() => {
            const modal = document.querySelector("[role='dialog'][aria-modal='true']");
            if (!modal) return null;
            const style = getComputedStyle(modal);
            const r = modal.getBoundingClientRect();
            return { position: style.position, top: r.top, left: r.left, width: r.width, height: r.height };
        }"""
    )
    assert g is not None
    fills = (
        g["position"] == "fixed"
        and g["top"] <= 1
        and g["left"] <= 1
        and g["width"] >= 374
        and g["height"] >= 811
    )
    evidence.shot("below 1024px the modal fills the screen", modal, held=fills)
    assert fills, g


def test_new_initiative_opens_in_the_same_centered_modal(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    """AC20: the board's 'New initiative' form opens in the same centered card modal over the dimmed board,
    and the old .panel aside is no longer rendered."""
    signed_in.set_viewport_size({"width": 1280, "height": 900})
    signed_in.goto(f"{server.url}/#/board")
    signed_in.locator("section.column").nth(5).wait_for()

    signed_in.get_by_role("button", name="New initiative").click()
    modal = _modal(signed_in)
    evidence.expect(modal, "New initiative opens in the card modal").to_be_visible()
    evidence.expect(modal, "the form is in the modal").to_contain_text("New initiative")
    evidence.expect(_backdrop(signed_in), "the board is dimmed behind the form").to_be_visible()
    evidence.expect(signed_in.locator(".panel"), "the old .panel aside is no longer rendered").to_have_count(
        0
    )


def test_add_child_opens_in_the_same_centered_modal(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    """AC20: 'Add child' (New story under a story) opens in the same centered card modal over the dimmed
    board, and the old .panel aside is no longer rendered."""
    open_modal(signed_in, server)  # opens the Approved story AIR-4
    modal = _modal(signed_in)

    modal.get_by_role("button", name="Add child").click()
    evidence.expect(modal, "Add child opens in the card modal").to_be_visible()
    evidence.expect(modal, "the form is in the modal").to_contain_text("New subtask")
    evidence.expect(_backdrop(signed_in), "the board is dimmed behind the form").to_be_visible()
    evidence.expect(signed_in.locator(".panel"), "the old .panel aside is no longer rendered").to_have_count(
        0
    )


def test_tab_stays_inside_the_modal(signed_in: Page, server: Server, evidence: Evidence) -> None:
    """AC21: with the modal open, repeated Tab keeps focus inside the modal (it never reaches the board
    behind the backdrop)."""
    open_modal(signed_in, server)
    modal = _modal(signed_in)

    # Press Tab many times; focus must never leave the modal.
    for _ in range(15):
        signed_in.keyboard.press("Tab")
        inside = signed_in.evaluate(
            """() => {
                const modal = document.querySelector("[role='dialog'][aria-modal='true']");
                return !!(modal && modal.contains(document.activeElement));
            }"""
        )
        if not inside:
            break

    inside = signed_in.evaluate(
        """() => {
            const modal = document.querySelector("[role='dialog'][aria-modal='true']");
            return !!(modal && modal.contains(document.activeElement));
        }"""
    )
    evidence.shot("repeated Tab stays inside the modal", modal, held=inside)
    assert inside


def test_the_board_has_no_panel_element(signed_in: Page, server: Server, evidence: Evidence) -> None:
    """AC22: the board screen has no element with class 'panel' (the 380px side panel is gone)."""
    signed_in.set_viewport_size({"width": 1280, "height": 900})
    signed_in.goto(f"{server.url}/#/board")
    signed_in.locator("section.column").nth(5).wait_for()

    evidence.expect(signed_in.locator(".panel"), "no element with class 'panel'").to_have_count(0)
