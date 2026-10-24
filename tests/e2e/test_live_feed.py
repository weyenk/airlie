"""The live feed in Chromium: a page that holds `GET /v1/events` open hears a board change the moment it is
made, whether the write came through the API (published at once) or from another process (a card written
straight into <state>/board.sqlite, as the story tick or `task board:import-jira` does, which the server's
watcher catches within about a second). The page reads the stream itself and keeps the first change it sees.

The feed has no replay, so each test waits for `: connected` before it writes."""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page

from airlie.board.store import BoardStore
from tests.e2e.conftest import KEY, Evidence, Server

pytestmark = pytest.mark.e2e

FIRST_CHANGE_JS = """key => {
  window.firstChange = null; window.connected = false;
  fetch("/v1/events", { headers: { Authorization: "Bearer " + key } }).then(async (r) => {
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) return;
      buf += dec.decode(value, { stream: true });
      if (buf.includes(": connected")) window.connected = true;
      const m = buf.match(/event: change\\ndata: (.*)\\n\\n/); if (m) { window.firstChange = m[1]; return; }
    }
  });
}"""
"""Run in the page: opens the stream with the admin key, sets `window.connected` once the server has said
`: connected`, and `window.firstChange` to the data line of the first `change` event."""


def _listen(page: Page, server: Server) -> None:
    """Open the app, start the page's own read of the stream, and wait until the server has connected it."""
    page.goto(server.url + "/next/")
    page.evaluate(FIRST_CHANGE_JS, KEY)
    page.wait_for_function("() => window.connected", timeout=3000)  # no replay: write only once connected


def _first_change(page: Page) -> dict[str, object]:
    page.wait_for_function("() => window.firstChange", timeout=3000)
    change: dict[str, object] = json.loads(page.evaluate("() => window.firstChange"))
    return change


def test_a_board_write_through_the_api_reaches_an_open_page(
    page: Page, own_server: Server, evidence: Evidence
) -> None:
    _listen(page, own_server)
    reply = httpx.post(
        own_server.url + "/v1/board/items",
        json={"level": "initiative", "title": "live"},
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert reply.status_code == 201, reply.text
    change = _first_change(page)
    key = change["key"]
    assert change["topic"] == "board" and isinstance(key, str) and key.startswith("AIR-")
    evidence.shot("a board write through the API reached the open page")


def test_a_write_from_another_process_reaches_an_open_page_within_two_seconds(
    page: Page, own_server: Server, evidence: Evidence
) -> None:
    _listen(page, own_server)
    store = BoardStore(own_server.state / "board.sqlite")
    try:
        store.create(level="initiative", title="tick")
    finally:
        store.close()
    # another connection's commit is only a hint that the board changed: no key, the page refetches the lot
    assert _first_change(page) == {"topic": "board", "key": None}
    evidence.shot("a write from another process reached the open page")
