"""The Vue app's Board in Chromium (/next/board): six columns that fit the window, a card that rings when
another process changes it, the phone's status pills, and a long title that cannot break a card. Each check
leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

from airlie.board.store import BoardStore
from tests.e2e.conftest import Evidence, Server

pytestmark = pytest.mark.e2e

COLUMNS = re.compile("^(To do|Approved|Doing|Review|Blocked|Done)$")


def test_each_status_has_a_column_and_columns_fit_the_window(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    next_page.goto(server.url + "/next/board")
    columns = next_page.get_by_role("region", name=COLUMNS)
    evidence.expect(columns, "six columns").to_have_count(6)
    box = next_page.locator("[data-columns]").bounding_box()
    viewport = next_page.viewport_size
    assert box is not None and viewport is not None
    held = box["y"] + box["height"] <= viewport["height"] - 16
    grid = next_page.locator("[data-columns]")
    evidence.shot("the columns end above the window's bottom edge", grid, held=held)
    assert held, box
    page_scrolls = next_page.evaluate("document.documentElement.scrollHeight > innerHeight + 1")
    evidence.shot("the page itself does not scroll down", held=not page_scrolls)
    assert not page_scrolls


def test_a_change_from_another_process_rings_its_card(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    next_page.goto(own_server.url + "/next/board")
    evidence.expect(next_page.get_by_text("Live", exact=True), "the feed is live").to_be_visible()
    card = next_page.locator("[data-item=AIR-3]")
    evidence.expect(card, "the card starts in To do, quiet").to_be_visible()
    evidence.expect(card.locator("[data-fresh-note]"), "no ring before the change").to_have_count(0)
    store = BoardStore(own_server.state / "board.sqlite")
    try:
        store.move("AIR-3", "doing")  # as the story tick would: straight into the file, no request
    finally:
        store.close()
    moved = next_page.get_by_role("region", name="Doing").locator("[data-item=AIR-3]")
    evidence.expect(moved, "the card is now in Doing").to_be_visible()
    note = moved.locator("[data-fresh-note]")
    evidence.expect(note, "it says updated just now").to_have_text("updated just now")
    evidence.expect(note, "the ring fades after about 4 s").to_have_count(0, timeout=8000)


def test_the_phone_board_shows_one_column_and_the_status_pills(
    next_page: Page, server: Server, evidence: Evidence
) -> None:
    next_page.set_viewport_size({"width": 375, "height": 812})
    next_page.goto(server.url + "/next/board")
    pills = next_page.locator("[data-pills]")
    evidence.expect(pills, "the status pills show").to_be_visible()
    evidence.expect(pills.get_by_role("button"), "one pill per status").to_have_count(6)
    evidence.expect(next_page.get_by_role("region", name=COLUMNS), "one column at a time").to_have_count(1)
    evidence.expect(next_page.get_by_role("region", name="Doing"), "Doing first").to_be_visible()
    pills.get_by_role("button", name=re.compile("^Done")).click()
    done = next_page.get_by_role("region", name="Done")
    evidence.expect(done, "choosing Done swaps the column").to_be_visible()
    evidence.expect(next_page.get_by_role("region", name=COLUMNS), "still one column").to_have_count(1)
    sideways = next_page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
    evidence.shot("the phone page does not scroll sideways", held=not sideways)
    assert not sideways


def test_a_long_title_does_not_break_its_card(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    title = "w" * 200
    store = BoardStore(own_server.state / "board.sqlite")
    try:
        store.create(level="story", title=title, labels=["repo:" + "o" * 100 + "/name"])
    finally:
        store.close()
    next_page.goto(own_server.url + "/next/board")
    card = next_page.locator("[data-item]", has=next_page.locator(f"[title='{title}']"))
    evidence.expect(card, "the card with the 200-character title").to_be_visible()
    geometry = card.evaluate(
        """c => ({ card: c.getBoundingClientRect().width,
                   column: c.closest('section').getBoundingClientRect().width,
                   overflow: c.scrollWidth - c.clientWidth })"""
    )
    held = geometry["card"] <= geometry["column"] and geometry["overflow"] <= 0
    evidence.shot("the card stays inside its column", card, held=held)
    assert held, geometry


def test_a_card_is_a_link_that_enter_follows(next_page: Page, server: Server, evidence: Evidence) -> None:
    next_page.goto(server.url + "/next/board")
    card = next_page.locator("[data-item=AIR-3]")
    evidence.expect(card, "the card is a link to its own address").to_have_attribute(
        "href", "/next/board/AIR-3"
    )
    card.focus()
    next_page.keyboard.press("Enter")
    evidence.expect(next_page, "Enter on the focused card goes to it").to_have_url(
        re.compile(r"/next/board/AIR-3$")
    )


def test_dragging_a_card_onto_another_moves_it_above_it(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    next_page.goto(own_server.url + "/next/board")
    evidence.expect(
        next_page.get_by_role("region", name="Doing").locator("[data-item]"), "Doing has two"
    ).to_have_count(2)
    next_page.locator("[data-item=AIR-3]").drag_to(next_page.locator("[data-item=AIR-2]"))
    doing = next_page.get_by_role("region", name="Doing").locator("[data-item]")
    evidence.expect(doing, "the card joined Doing").to_have_count(3)
    keys = [c.get_attribute("data-item") for c in doing.all()]
    evidence.shot("it sits above the card it was dropped on", held=keys == ["AIR-1", "AIR-3", "AIR-2"])
    assert keys == ["AIR-1", "AIR-3", "AIR-2"], keys


def test_the_landing_mark_stays_put_over_itself_and_the_gaps(
    next_page: Page, own_server: Server, evidence: Evidence
) -> None:
    """Real hit-testing: the mark takes no pointer, so over it and the 8px gaps the pointer is on the list."""
    next_page.goto(own_server.url + "/next/board")
    doing = next_page.get_by_role("region", name="Doing")
    evidence.expect(doing.locator("[data-item]"), "Doing has two").to_have_count(2)

    def centre(box: dict[str, float]) -> tuple[float, float]:
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def below(key: str) -> str | None:
        return doing.locator("[data-drop]").evaluate("m => m.nextElementSibling?.getAttribute('data-item')")

    src = next_page.locator("[data-item=AIR-3]").bounding_box()
    two = doing.locator("[data-item=AIR-2]").bounding_box()
    assert src and two
    mouse = next_page.mouse
    mouse.move(*centre(src))
    mouse.down()
    mouse.move(*centre(src), steps=2)
    mouse.move(*centre(two), steps=10)
    evidence.expect(doing.locator("[data-drop]"), "the mark shows above AIR-2").to_have_count(1)
    assert below("AIR-2") == "AIR-2"
    mark = doing.locator("[data-drop]").bounding_box()
    one = doing.locator("[data-item=AIR-1]").bounding_box()
    assert mark and one
    mouse.move(*centre(mark), steps=5)  # over the mark itself
    assert below("AIR-2") == "AIR-2", "the mark moved when the pointer was on it"
    gap_y = one["y"] + one["height"] + (mark["y"] - one["y"] - one["height"]) / 2
    mouse.move(centre(mark)[0], gap_y, steps=5)  # over the gap between AIR-1 and the mark
    assert below("AIR-2") == "AIR-2", "the mark moved when the pointer was in the gap"
    evidence.shot("the mark stayed above AIR-2 over itself and the gap", doing.locator("[data-drop]"))
    mouse.up()
    cards = doing.locator("[data-item]")
    evidence.expect(cards, "the card joined Doing").to_have_count(3)
    keys = [c.get_attribute("data-item") for c in cards.all()]
    evidence.shot("a drop there landed above AIR-2", held=keys == ["AIR-1", "AIR-3", "AIR-2"])
    assert keys == ["AIR-1", "AIR-3", "AIR-2"], keys
