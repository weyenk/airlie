"""/v1/board under task serve: items by level and parent, moves, comments, the export, and the serve key."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from airlie.board import BoardStore
from airlie.serve.app import create_app
from tests.unit.test_serve import FakeUpstream, cfg


def client(tmp_path: Path, **kw: object) -> TestClient:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), **kw)  # type: ignore[arg-type]
    return TestClient(app)


def test_items_are_created_listed_by_parent_and_moved(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        r = c.post("/v1/board/items", json={"level": "initiative", "title": "Remote studio"})
        assert r.status_code == 201, r.text
        init = r.json()
        assert init["key"] == "AL-1" and init["status"] == "todo" and init["parent"] is None
        epic = c.post(
            "/v1/board/items",
            json={"level": "epic", "title": "Board", "parent": "AL-1", "labels": ["repo:weyenk/airlie"]},
        ).json()
        story = c.post(
            "/v1/board/items",
            json={"level": "story", "title": "Store", "description": "d", "parent": epic["key"]},
        ).json()
        assert story["key"] == "AL-3" and story["labels"] == []
        assert [i["key"] for i in c.get("/v1/board/items", params={"parent": "AL-1"}).json()["items"]] == [
            "AL-2"
        ]
        assert [i["key"] for i in c.get("/v1/board/items", params={"level": "story"}).json()["items"]] == [
            "AL-3"
        ]
        assert len(c.get("/v1/board/items").json()["items"]) == 3
        moved = c.patch("/v1/board/items/AL-3", json={"status": "doing"})
        assert moved.status_code == 200 and moved.json()["status"] == "doing"
        assert c.get("/v1/board/items/AL-3").json()["status"] == "doing"
        assert [i["key"] for i in c.get("/v1/board/items", params={"status": "doing"}).json()["items"]] == [
            "AL-3"
        ]
        renamed = c.patch("/v1/board/items/AL-3", json={"title": "Store v2", "labels": ["x"]}).json()
        assert (renamed["title"], renamed["labels"], renamed["status"]) == ("Store v2", ["x"], "doing")


def test_a_parent_two_levels_up_is_refused_and_not_stored(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        c.post("/v1/board/items", json={"level": "initiative", "title": "i"})
        r = c.post("/v1/board/items", json={"level": "subtask", "title": "s", "parent": "AL-1"})
        assert r.status_code == 422 and "AL-1 is an initiative" in r.json()["detail"]
        assert len(c.get("/v1/board/items").json()["items"]) == 1
        epic = c.post("/v1/board/items", json={"level": "epic", "title": "e", "parent": "AL-1"}).json()
        other = c.post("/v1/board/items", json={"level": "initiative", "title": "i2"}).json()
        story = c.post("/v1/board/items", json={"level": "story", "title": "s", "parent": epic["key"]}).json()
        bad = c.patch(f"/v1/board/items/{story['key']}", json={"parent": other["key"]})
        assert bad.status_code == 422 and "is an initiative" in bad.json()["detail"]
        assert c.get(f"/v1/board/items/{story['key']}").json()["parent"] == epic["key"]
        assert c.post("/v1/board/items", json={"level": "saga", "title": "x"}).status_code == 422
        assert c.patch(f"/v1/board/items/{story['key']}", json={"status": "shipped"}).status_code == 422
        assert (
            c.post("/v1/board/items", json={"level": "initiative", "title": "x", "nope": 1}).status_code
            == 422
        )


def test_an_unknown_key_is_404(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        assert c.get("/v1/board/items/AL-9").status_code == 404
        assert c.patch("/v1/board/items/AL-9", json={"title": "x"}).status_code == 404
        assert c.post("/v1/board/items/AL-9/comments", json={"text": "x"}).status_code == 404
        r = c.post("/v1/board/items", json={"level": "epic", "title": "e", "parent": "AL-9"})
        assert r.status_code == 422 and "AL-9" in r.json()["detail"]


def test_comments_are_by_noah_and_the_export_has_everything(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        c.post("/v1/board/items", json={"level": "initiative", "title": "i"})
        r = c.post("/v1/board/items/AL-1/comments", json={"text": "looks good"})
        assert r.status_code == 200
        [comment] = r.json()["comments"]
        assert comment["author"] == "noah" and comment["text"] == "looks good"
        out = c.get("/v1/board/export").json()
        assert out["prefix"] == "AL" and out["items"] == [c.get("/v1/board/items/AL-1").json()]


def test_the_board_is_in_state_dir_and_survives_a_restart(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        c.post("/v1/board/items", json={"level": "initiative", "title": "i"})
    store = BoardStore(tmp_path / "state" / "board.sqlite")
    assert store.get("AL-1").title == "i"
    store.close()
    with client(tmp_path) as c:
        assert c.post("/v1/board/items", json={"level": "initiative", "title": "j"}).json()["key"] == "AL-2"


def test_the_serve_key_guards_the_board(tmp_path: Path) -> None:
    with client(tmp_path, api_key="k") as c:
        assert c.get("/v1/board/items").status_code == 401
        ok = c.get("/v1/board/items", headers={"Authorization": "Bearer k"})
        assert ok.status_code == 200 and ok.json() == {"items": []}


def test_building_the_app_does_not_touch_the_board_file(tmp_path: Path) -> None:
    create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    assert not (tmp_path / "state" / "board.sqlite").exists()
