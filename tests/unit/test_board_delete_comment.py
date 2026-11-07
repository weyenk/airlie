"""DELETE /v1/board/items/{key}/comments/{id}: the signed-in user deletes their own comment; a non-author is
refused with 403 and the phrase 'can only delete your own comments' (AC2); an unauthenticated request is 401
and the phrase 'not signed in' (AC3). The tests run through create_app(..., api_key=...) so the principal
middleware runs: with the api-key principal, name and user_id are both 'api-key', so a comment is the
caller's only when its author is 'api-key' — never the PERSON constant, which would make the wrong-reason
version pass."""

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


def test_a_non_author_is_refused_with_403_and_the_comment_is_untouched(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        item = _item(tmp_path, c)
        key = str(item["key"])
        store = BoardStore(tmp_path / "state" / "board.sqlite")
        try:
            # a comment by someone else: the api-key principal is 'api-key', so 'airlie' is not them
            store.comment(key, "airlie", "someone else's note")
        finally:
            store.close()
        r = c.delete(f"/v1/board/items/{key}/comments/1", headers=AUTH)
        assert r.status_code == 403, r.text
        assert "you can only delete your own comments" in r.text
        # the refusal changes nothing: the comment is still the other person's, with their text
        got = c.get(f"/v1/board/items/{key}", headers=AUTH).json()
        [comment] = got["comments"]
        assert (
            comment["id"] == 1 and comment["author"] == "airlie" and comment["text"] == "someone else's note"
        )


def test_an_unauthenticated_request_is_401_and_the_comment_is_untouched(tmp_path: Path) -> None:
    with client(tmp_path) as c:
        item = _item(tmp_path, c)
        key = str(item["key"])
        store = BoardStore(tmp_path / "state" / "board.sqlite")
        try:
            store.comment(key, "airlie", "a note")
        finally:
            store.close()
        # no Authorization header: the serve middleware refuses before the route runs
        r = c.delete(f"/v1/board/items/{key}/comments/1")
        assert r.status_code == 401, r.text
        assert "not signed in" in r.text
        # the refusal changes nothing: the comment is still there, with its author and text
        got = c.get(f"/v1/board/items/{key}", headers=AUTH).json()
        [comment] = got["comments"]
        assert comment["id"] == 1 and comment["author"] == "airlie" and comment["text"] == "a note"
