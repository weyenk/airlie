"""PATCH /v1/board/items/{key}/comments/{id}: the signed-in user edits their own comment; a non-author is
refused with 403 and the phrase 'can only edit your own comments' (AC16); an unknown comment id is 404.
The tests run through create_app(..., api_key=...) so the principal middleware runs: with the api-key
principal, name and user_id are both 'api-key', so a comment is the caller's only when its author is
'api-key' — never the PERSON constant, which would make the wrong-reason version pass."""

from __future__ import annotations

from pathlib import Path

import httpx

from airlie.board import BoardStore
from airlie.serve.app import create_app
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream, cfg

API_KEY = "k"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


def client(tmp_path: Path) -> TestClient:
    app = create_app(
        cfg(tmp_path),
        transport=httpx.MockTransport(FakeUpstream()),
        api_key=API_KEY,
    )  # type: ignore[arg-type]
    return TestClient(app)


def _item(tmp_path: Path, c: TestClient) -> dict[str, object]:
    """One item on the board, created through the API so the app's store and the test's store agree."""
    r = c.post("/v1/board/items", json={"level": "initiative", "title": "i"}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


def test_a_non_author_is_refused_with_403_and_the_phrase(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        item = _item(tmp_path, c)
        key = str(item["key"])
        store = BoardStore(tmp_path / "state" / "board.sqlite")
        try:
            # a comment by someone else: the api-key principal is 'api-key', so 'airlie' is not them
            store.comment(key, "airlie", "someone else's note")
        finally:
            store.close()
        r = c.patch(f"/v1/board/items/{key}/comments/1", json={"text": "hijacked"}, headers=AUTH)
        assert r.status_code == 403, r.text
        assert "can only edit your own comments" in r.text
        # the refusal changes nothing: the comment is still the other person's, with their text
        got = c.get(f"/v1/board/items/{key}", headers=AUTH).json()
        [comment] = got["comments"]
        assert comment["author"] == "airlie" and comment["text"] == "someone else's note"


def test_the_author_edits_their_own_comment(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        item = _item(tmp_path, c)
        key = str(item["key"])
        store = BoardStore(tmp_path / "state" / "board.sqlite")
        try:
            # the api-key principal's name and user_id are both 'api-key'
            store.comment(key, "api-key", "first draft")
        finally:
            store.close()
        r = c.patch(f"/v1/board/items/{key}/comments/1", json={"text": "revised"}, headers=AUTH)
        assert r.status_code == 200, r.text
        [comment] = r.json()["comments"]
        assert comment["id"] == 1 and comment["author"] == "api-key" and comment["text"] == "revised"
        # the edit is stored, not just echoed
        got = c.get(f"/v1/board/items/{key}", headers=AUTH).json()
        [stored] = got["comments"]
        assert stored["text"] == "revised"


def test_an_unknown_comment_id_is_404(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        item = _item(tmp_path, c)
        key = str(item["key"])
        r = c.patch(f"/v1/board/items/{key}/comments/99", json={"text": "x"}, headers=AUTH)
        assert r.status_code == 404, r.text
        # the specific 'no comment' message, not the generic 404 a missing route would give
        assert f"no comment 99 on {key}" in r.json()["detail"]
