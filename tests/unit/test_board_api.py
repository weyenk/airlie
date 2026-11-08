"""/v1/board under task serve: items by level and parent, moves, comments, the export, and the serve key."""

from __future__ import annotations

from pathlib import Path

import httpx

from airlie.board import BoardStore
from airlie.flow.spec import check_workflows
from airlie.serve.app import create_app
from airlie.story.workflows import draft_spec
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream, cfg
from tests.unit.test_story_workflows import MINI


def client(tmp_path: Path, **kw: object) -> TestClient:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), **kw)  # type: ignore[arg-type]
    return TestClient(app)


def test_items_are_created_listed_by_parent_and_moved(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        r = c.post("/v1/board/items", json={"level": "initiative", "title": "Remote studio"})
        assert r.status_code == 201, r.text
        init = r.json()
        assert init["key"] == "AIR-1" and init["status"] == "todo" and init["parent"] is None
        epic = c.post(
            "/v1/board/items",
            json={"level": "epic", "title": "Board", "parent": "AIR-1", "labels": ["repo:weyenk/airlie"]},
        ).json()
        story = c.post(
            "/v1/board/items",
            json={"level": "story", "title": "Store", "description": "d", "parent": epic["key"]},
        ).json()
        assert story["key"] == "AIR-3" and story["labels"] == []
        assert [i["key"] for i in c.get("/v1/board/items", params={"parent": "AIR-1"}).json()["items"]] == [
            "AIR-2"
        ]
        assert [i["key"] for i in c.get("/v1/board/items", params={"level": "story"}).json()["items"]] == [
            "AIR-3"
        ]
        assert len(c.get("/v1/board/items").json()["items"]) == 3
        moved = c.patch("/v1/board/items/AIR-3", json={"status": "doing"})
        assert moved.status_code == 200 and moved.json()["status"] == "doing"
        assert c.get("/v1/board/items/AIR-3").json()["status"] == "doing"
        assert [i["key"] for i in c.get("/v1/board/items", params={"status": "doing"}).json()["items"]] == [
            "AIR-3"
        ]
        renamed = c.patch("/v1/board/items/AIR-3", json={"title": "Store v2", "labels": ["x"]}).json()
        assert (renamed["title"], renamed["labels"], renamed["status"]) == ("Store v2", ["x"], "doing")


def test_a_parent_two_levels_up_is_refused_and_not_stored(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        c.post("/v1/board/items", json={"level": "initiative", "title": "i"})
        r = c.post("/v1/board/items", json={"level": "subtask", "title": "s", "parent": "AIR-1"})
        assert r.status_code == 422 and "AIR-1 is an initiative" in r.json()["detail"]
        assert len(c.get("/v1/board/items").json()["items"]) == 1
        epic = c.post("/v1/board/items", json={"level": "epic", "title": "e", "parent": "AIR-1"}).json()
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
        assert c.get("/v1/board/items/AIR-9").status_code == 404
        assert c.patch("/v1/board/items/AIR-9", json={"title": "x"}).status_code == 404
        assert c.post("/v1/board/items/AIR-9/comments", json={"text": "x"}).status_code == 404
        r = c.post("/v1/board/items", json={"level": "epic", "title": "e", "parent": "AIR-9"})
        assert r.status_code == 422 and "AIR-9" in r.json()["detail"]


def test_comments_are_by_noah_and_the_export_has_everything(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        c.post("/v1/board/items", json={"level": "initiative", "title": "i"})
        r = c.post("/v1/board/items/AIR-1/comments", json={"text": "looks good"})
        assert r.status_code == 200
        [comment] = r.json()["comments"]
        assert comment["author"] == "noah" and comment["text"] == "looks good"
        out = c.get("/v1/board/export").json()
        one = c.get("/v1/board/items/AIR-1").json()
        assert one.pop("started") is False  # the one item says whether it has started; the export does not
        assert out["prefix"] == "AIR" and out["items"] == [one]


def test_the_board_is_in_state_dir_and_survives_a_restart(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        c.post("/v1/board/items", json={"level": "initiative", "title": "i"})
    store = BoardStore(tmp_path / "state" / "board.sqlite")
    assert store.get("AIR-1").title == "i"
    store.close()
    with client(tmp_path) as c:
        assert c.post("/v1/board/items", json={"level": "initiative", "title": "j"}).json()["key"] == "AIR-2"


def test_the_serve_key_guards_the_board(tmp_path: Path) -> None:
    with client(tmp_path, api_key="k") as c:
        assert c.get("/v1/board/items").status_code == 401
        ok = c.get("/v1/board/items", headers={"Authorization": "Bearer k"})
        assert ok.status_code == 200 and ok.json() == {"items": []}


def test_building_the_app_does_not_touch_the_board_file(tmp_path: Path) -> None:
    create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    assert not (tmp_path / "state" / "board.sqlite").exists()


def test_move_places_a_card_in_a_column(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        for t in "abc":
            c.post("/v1/board/items", json={"level": "initiative", "title": t})
        r = c.post("/v1/board/items/AIR-3/move", json={"status": "doing"})
        assert r.status_code == 200 and r.json()["status"] == "doing"
        c.post("/v1/board/items/AIR-1/move", json={"status": "doing", "before": "AIR-3"})
        keys = [i["key"] for i in c.get("/v1/board/items", params={"status": "doing"}).json()["items"]]
        assert keys == ["AIR-1", "AIR-3"]
        assert (
            c.post("/v1/board/items/AIR-2/move", json={"status": "doing", "before": "AIR-9"}).status_code
            == 422
        )
        assert c.post("/v1/board/items/AIR-9/move", json={"status": "doing"}).status_code == 404
        assert c.post("/v1/board/items/AIR-2/move", json={"status": "shipped"}).status_code == 422


def test_a_move_through_the_api_is_recorded_as_the_principals(tmp_path: Path) -> None:
    """The principal's name is who moved it (planning-line P5b, D9); the bootstrap admin on loopback is the
    one person."""
    with client(tmp_path) as c:
        key = c.post("/v1/board/items", json={"level": "story", "title": "S"}).json()["key"]
        assert c.patch(f"/v1/board/items/{key}", json={"status": "approved"}).status_code == 200
        assert c.post(f"/v1/board/items/{key}/move", json={"status": "doing"}).status_code == 200
        assert c.patch(f"/v1/board/items/{key}", json={"title": "renamed"}).status_code == 200  # no move
    moves = BoardStore(tmp_path / "state" / "board.sqlite").moves(key)
    assert [(m.from_status, m.to_status) for m in moves] == [("todo", "approved"), ("approved", "doing")]
    assert {m.by for m in moves} == {moves[0].by} and moves[0].by != "line"


# ---- the card's workflow (docs/superpowers/specs/2026-11-07-workflow-builder-design.md) ----

NO_RUN = MINI.replace("intake: {type: check, run: story.intake}", "intake: {type: check}")


def test_a_label_naming_an_unknown_workflow_is_refused_with_422(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        story = c.post("/v1/board/items", json={"level": "story", "title": "s"}).json()["key"]
        r = c.patch(f"/v1/board/items/{story}", json={"labels": ["repo:o/r", "workflow:nope"]})
        assert r.status_code == 422 and r.json()["detail"] == "no workflow named 'nope'"
        assert c.get(f"/v1/board/items/{story}").json()["labels"] == []  # nothing stored
        # a card with no workflow label runs the story's: never a problem
        assert c.patch(f"/v1/board/items/{story}", json={"labels": ["repo:o/r"]}).status_code == 200
        assert c.post(f"/v1/board/items/{story}/move", json={"status": "approved"}).status_code == 200


def test_moving_a_story_with_an_invalid_draft_to_approved_is_refused(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        assert c.put("/v1/workflows/drafts/mini", json={"type": "bug", "yaml": MINI}).status_code == 200
        story = c.post("/v1/board/items", json={"level": "story", "title": "s"}).json()["key"]
        assert c.patch(f"/v1/board/items/{story}", json={"labels": ["workflow:mini"]}).status_code == 200
        assert c.put("/v1/workflows/drafts/mini", json={"type": "bug", "yaml": NO_RUN}).status_code == 200
        [problem] = check_workflows({"mini": draft_spec("mini", NO_RUN)})
        r = c.post(f"/v1/board/items/{story}/move", json={"status": "approved"})
        assert r.status_code == 422 and problem in r.json()["detail"]
        assert c.get(f"/v1/board/items/{story}").json()["status"] == "todo"
        # only approval runs it: any other column takes the card as it is
        assert c.post(f"/v1/board/items/{story}/move", json={"status": "doing"}).status_code == 200
        assert c.post(f"/v1/board/items/{story}/move", json={"status": "todo"}).status_code == 200
        # a PATCH of the status is the other way into approved: refused the same
        r = c.patch(f"/v1/board/items/{story}", json={"status": "approved"})
        assert r.status_code == 422 and problem in r.json()["detail"]
        assert c.get(f"/v1/board/items/{story}").json()["status"] == "todo"


def test_a_label_change_that_keeps_the_workflow_is_not_checked(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        assert c.put("/v1/workflows/drafts/mini", json={"type": "bug", "yaml": MINI}).status_code == 200
        story = c.post("/v1/board/items", json={"level": "story", "title": "s"}).json()["key"]
        assert c.patch(f"/v1/board/items/{story}", json={"labels": ["workflow:mini"]}).status_code == 200
        assert c.put("/v1/workflows/drafts/mini", json={"type": "bug", "yaml": NO_RUN}).status_code == 200
        # another label added beside it: the workflow is the same, so its problem is not this edit's
        r = c.patch(f"/v1/board/items/{story}", json={"labels": ["workflow:mini", "repo:o/r"]})
        assert r.status_code == 200 and r.json()["labels"] == ["workflow:mini", "repo:o/r"]


STARTED = "the workflow of a started card cannot be changed; make a new card for the other workflow"


def test_the_workflow_of_a_started_card_cannot_be_changed(tmp_path: Path) -> None:
    """A card with a saved state started on its workflow (D21): adding, removing or changing its `workflow:`
    label is refused; its other labels change as ever, and the item says it has started."""
    with client(tmp_path) as c:
        assert c.put("/v1/workflows/drafts/mini", json={"type": "bug", "yaml": MINI}).status_code == 200
        plain = c.post("/v1/board/items", json={"level": "story", "title": "plain"}).json()["key"]
        mini = c.post(
            "/v1/board/items", json={"level": "story", "title": "mini", "labels": ["workflow:mini"]}
        ).json()["key"]
        fresh = c.post("/v1/board/items", json={"level": "story", "title": "fresh"}).json()["key"]
        for key in (plain, mini):
            (tmp_path / "state" / "stories" / key).mkdir(parents=True)
            (tmp_path / "state" / "stories" / key / "state.json").write_text('{"stage": "intake"}')
        assert c.get(f"/v1/board/items/{plain}").json()["started"] is True
        assert c.get(f"/v1/board/items/{fresh}").json()["started"] is False
        for key, labels in (
            (plain, ["workflow:mini"]),  # added
            (mini, []),  # removed
            (mini, ["workflow:story"]),  # changed
        ):
            r = c.patch(f"/v1/board/items/{key}", json={"labels": labels})
            assert (r.status_code, r.json()["detail"]) == (422, STARTED), (key, labels)
        assert c.get(f"/v1/board/items/{mini}").json()["labels"] == ["workflow:mini"]
        assert c.patch(f"/v1/board/items/{mini}", json={"labels": ["workflow:mini", "x"]}).status_code == 200
        assert c.patch(f"/v1/board/items/{plain}", json={"labels": ["x"]}).status_code == 200
        assert c.patch(f"/v1/board/items/{fresh}", json={"labels": ["workflow:mini"]}).status_code == 200


def test_delete_removes_the_principals_comment_and_leaves_the_other_unchanged(tmp_path: Path) -> None:
    """DELETE the signed-in user's own comment: 200, the deleted id gone, the other comment untouched
    (AC1)."""
    with client(tmp_path, api_key="k") as c:
        auth = {"Authorization": "Bearer k"}
        key = c.post("/v1/board/items", json={"level": "initiative", "title": "i"}, headers=auth).json()[
            "key"
        ]
        store = BoardStore(tmp_path / "state" / "board.sqlite")
        try:
            # the api-key principal's name and user_id are both 'api-key'
            store.comment(key, "api-key", "mine")
            store.comment(key, "airlie", "theirs")
        finally:
            store.close()
        before = c.get(f"/v1/board/items/{key}", headers=auth).json()["comments"]
        assert [(c_["id"], c_["author"]) for c_ in before] == [(1, "api-key"), (2, "airlie")]
        r = c.delete(f"/v1/board/items/{key}/comments/1", headers=auth)
        assert r.status_code == 200, r.text
        comments = r.json()["comments"]
        assert [c_["id"] for c_ in comments] == [2]  # the deleted id is gone, the other remains
        assert comments[0] == before[1]  # the other comment, id, author, text and at, unchanged
        got = c.get(f"/v1/board/items/{key}", headers=auth).json()
        assert got["comments"] == comments  # the delete is stored, not just echoed


def test_deleting_one_of_two_comments_leaves_the_other_byte_for_byte(tmp_path: Path) -> None:
    """The remaining comment is byte-for-byte the other author's: id, author, text and at all unchanged
    (AC6)."""
    with client(tmp_path, api_key="k") as c:
        auth = {"Authorization": "Bearer k"}
        key = c.post("/v1/board/items", json={"level": "initiative", "title": "i"}, headers=auth).json()[
            "key"
        ]
        store = BoardStore(tmp_path / "state" / "board.sqlite")
        try:
            store.comment(key, "api-key", "mine")
            store.comment(key, "airlie", "theirs")
        finally:
            store.close()
        [other] = c.get(f"/v1/board/items/{key}", headers=auth).json()["comments"][1:]
        r = c.delete(f"/v1/board/items/{key}/comments/1", headers=auth)
        assert r.status_code == 200, r.text
        [remaining] = r.json()["comments"]
        assert remaining == other  # id, author, text and at, byte for byte
