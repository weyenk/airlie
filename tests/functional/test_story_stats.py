"""The line-stats route through its TestClient (planning-line P5b, D9): /v1/airlie/line/stats answers the
numbers line_stats computes, narrows them with `since`, refuses a `since` that is not a date, and needs a
key. The numbers themselves are pinned in tests/unit/test_story_stats.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx

from airlie.board import BoardStore
from airlie.serve.app import create_app
from tests.support.serve import FakeUpstream
from tests.support.serve import cfg as serve_cfg
from tests.unit.serve_helpers import TestClient


def make_story(store: BoardStore, title: str = "s") -> str:
    return store.create(level="story", title=title).key


def go(store: BoardStore, key: str, moves: list[tuple[str, str, float]]) -> None:
    """Moves as (status, by, time), in order, and comments as ("comment", author, time)."""
    for status, by, stamp in moves:
        if status == "comment":
            store.comment(key, by, "a word", at=stamp)
        else:
            store.move(key, status, by=by)  # type: ignore[arg-type]
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE moves SET at = ? WHERE id = (SELECT MAX(id) FROM moves)", (stamp,))


def test_the_stats_route(tmp_path: Path) -> None:
    c = serve_cfg(tmp_path)
    store = BoardStore(c.observability.state_dir / "board.sqlite")
    key = make_story(store)
    go(store, key, [("approved", "noah", 10), ("doing", "line", 11), ("review", "line", 50)])
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    auth = {"Authorization": "Bearer k"}
    with TestClient(app) as client:
        body = client.get("/v1/airlie/line/stats", headers=auth).json()
        assert set(body) == {"unattended", "blocks", "hours", "amigos", "budgets"}
        assert body["unattended"] == {"stories": 1, "unattended": 1, "rate": 1.0}
        later = client.get("/v1/airlie/line/stats", params={"since": "2026-12-01"}, headers=auth).json()
        assert later["unattended"]["stories"] == 0  # the review was at epoch 50
        assert (
            client.get("/v1/airlie/line/stats", params={"since": "yesterday"}, headers=auth).status_code
            == 400
        )
        assert client.get("/v1/airlie/line/stats").status_code == 401
