"""AIR-16 in Chromium: the six columns share the page width in one row, between 180px and 360px each, scroll
sideways when the panel leaves less than that, snap at phone width, and no card is wider than its column. Each
check leaves its screenshot in test-results/evidence/ (conftest.Evidence)."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import Evidence, Server

pytestmark = pytest.mark.e2e

GEOMETRY = """() => {
  const grid = document.querySelector(".columns");
  const cols = [...document.querySelectorAll("section.column")].map((c) => c.getBoundingClientRect());
  const items = [...document.querySelectorAll(".item")].map((i) => ({
    width: i.getBoundingClientRect().width,
    column: i.closest("section.column").getBoundingClientRect().width,
    overflow: i.scrollWidth - i.clientWidth,
  }));
  const style = getComputedStyle(grid);
  return {
    tops: cols.map((r) => Math.round(r.top)),
    widths: cols.map((r) => r.width),
    gridWidth: grid.clientWidth,
    scrolls: grid.scrollWidth > grid.clientWidth + 1,
    overflowX: style.overflowX,
    snap: style.scrollSnapType,
    columnSnap: getComputedStyle(document.querySelector("section.column")).scrollSnapAlign,
    items,
  };
}"""


def board(page: Page, server: Server, width: int, path: str = "#/board") -> dict[str, Any]:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{server.url}/{path}")
    page.locator("section.column").nth(5).wait_for()
    if path != "#/board":
        page.locator("aside.panel").wait_for()
    result: dict[str, Any] = page.evaluate(GEOMETRY)
    return result


def no_card_overflows(g: dict[str, Any]) -> bool:
    return all(i["width"] <= i["column"] + 0.5 and i["overflow"] <= 0 for i in g["items"])


@pytest.mark.parametrize("width", [1280, 1920, 2560])
def test_six_columns_share_one_row_between_180_and_360px(
    signed_in: Page, server: Server, evidence: Evidence, width: int
) -> None:
    g = board(signed_in, server, width)
    columns = signed_in.locator("section.column")
    one_row = len(set(g["tops"])) == 1
    evidence.shot(f"all six columns in one row at {width}px", columns, held=one_row)
    assert one_row, g["tops"]
    sized = all(180 <= w <= 360.5 for w in g["widths"])
    evidence.shot(
        f"each column 180-360px wide at {width}px: {[round(w) for w in g['widths']]}", columns, held=sized
    )
    assert sized, g["widths"]
    fills = sum(g["widths"]) >= min(g["gridWidth"], 6 * 360) * 0.9
    evidence.shot(f"the columns use the width there is at {width}px", columns, held=fills)
    assert fills, (sum(g["widths"]), g["gridWidth"])


@pytest.mark.parametrize("width", [1280, 1920, 2560])
def test_no_card_is_wider_than_its_column(
    signed_in: Page, server: Server, evidence: Evidence, width: int
) -> None:
    g = board(signed_in, server, width)
    held = no_card_overflows(g)
    evidence.shot(f"every card fits its column at {width}px", signed_in.locator(".item"), held=held)
    assert held, g["items"]


def test_with_the_panel_open_the_columns_scroll_sideways_instead_of_wrapping(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    g = board(signed_in, server, 1280, "#/board/AIR-4")
    columns = signed_in.locator("section.column")
    one_row = len(set(g["tops"])) == 1
    evidence.shot("panel open at 1280px: still one row", columns, held=one_row)
    assert one_row, g["tops"]
    sideways = g["overflowX"] in ("auto", "scroll") and (g["scrolls"] or g["gridWidth"] >= 6 * 180)
    evidence.shot("panel open at 1280px: the board scrolls sideways when narrow", columns, held=sideways)
    assert sideways, g
    held = no_card_overflows(g)
    evidence.shot("panel open at 1280px: every card fits its column", signed_in.locator(".item"), held=held)
    assert held, g["items"]


def test_at_phone_width_the_board_scrolls_sideways_and_snaps(
    signed_in: Page, server: Server, evidence: Evidence
) -> None:
    g = board(signed_in, server, 375)
    columns = signed_in.locator("section.column")
    held = g["scrolls"] and g["snap"].startswith("x") and "mandatory" in g["snap"]
    evidence.shot(f"375px: scrolls sideways with snapping ({g['snap']})", columns, held=held)
    assert held, g
    starts = g["columnSnap"] == "start"
    evidence.shot("375px: each column snaps to its start", columns, held=starts)
    assert starts, g["columnSnap"]
    fits = no_card_overflows(g)
    evidence.shot("375px: every card fits its column", signed_in.locator(".item"), held=fits)
    assert fits, g["items"]
