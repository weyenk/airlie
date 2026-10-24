"""Layer 01 of the board card-modal story: comments get a stable id and an edit method.

A comment is addressed by (item key, comment id) so a later layer can PATCH it in place.
The store is the user-facing seam here; the API and UI layers build on top of it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from airlie.board import BoardStore, Comment


def store(tmp_path: Path) -> BoardStore:
    return BoardStore(tmp_path / "board.sqlite")


def test_comment_carries_a_stable_id(tmp_path: Path) -> None:
    """A comment read back from the store carries its id, and it is stable across reads."""
    s = store(tmp_path)
    item = s.create(level="initiative", title="i")
    s.comment(item.key, "noah", "first")
    s.comment(item.key, "airlie", "second")
    comments = s.get(item.key).comments
    assert len(comments) == 2
    assert all(isinstance(c, Comment) for c in comments)
    ids = [c.id for c in comments]
    assert all(isinstance(i, int) for i in ids)
    assert len(set(ids)) == 2  # distinct, stable identities
    again = s.get(item.key).comments
    assert [c.id for c in again] == ids  # the same ids on a later read


def test_edit_comment_changes_the_stored_text(tmp_path: Path) -> None:
    """edit_comment rewrites the comment's text in place; the id, author and time are untouched."""
    s = store(tmp_path)
    item = s.create(level="initiative", title="i")
    made = s.comment(item.key, "noah", "original")
    cid = made.comments[0].id
    edited = s.edit_comment(item.key, cid, "rewritten")
    assert [c.text for c in edited.comments] == ["rewritten"]
    assert s.get(item.key).comments[0].text == "rewritten"  # persisted, not just returned
    c = s.get(item.key).comments[0]
    assert (c.id, c.author) == (cid, "noah")  # identity and author survive the edit
    assert c.at == made.comments[0].at  # the original time is kept


def test_edit_comment_bumps_updated_at(tmp_path: Path) -> None:
    """Editing a comment is an update to the item: its updated_at moves forward."""
    s = store(tmp_path)
    item = s.create(level="initiative", title="i")
    made = s.comment(item.key, "noah", "original")
    before = made.updated_at
    time.sleep(0.01)  # a real, tiny gap so the bump is strictly greater
    cid = made.comments[0].id
    edited = s.edit_comment(item.key, cid, "rewritten")
    assert edited.updated_at > before


def test_edit_comment_raises_key_error_for_an_unknown_item(tmp_path: Path) -> None:
    s = store(tmp_path)
    item = s.create(level="initiative", title="i")
    cid = s.comment(item.key, "noah", "x").comments[0].id
    with pytest.raises(KeyError):
        s.edit_comment("AIR-99", cid, "nope")


def test_edit_comment_raises_key_error_for_a_comment_on_another_item(tmp_path: Path) -> None:
    """A comment id is only valid on its own item: the same id on a different item is refused."""
    s = store(tmp_path)
    a = s.create(level="initiative", title="a")
    b = s.create(level="initiative", title="b")
    cid_a = s.comment(a.key, "noah", "on a").comments[0].id
    with pytest.raises(KeyError):
        s.edit_comment(b.key, cid_a, "not on b")
    assert s.get(a.key).comments[0].text == "on a"  # the original comment is untouched


def test_edit_comment_raises_key_error_for_an_unknown_comment_id(tmp_path: Path) -> None:
    """A comment id that is not on the item at all is refused, and nothing changes."""
    s = store(tmp_path)
    item = s.create(level="initiative", title="i")
    made = s.comment(item.key, "noah", "x")
    before = made.updated_at
    with pytest.raises(KeyError):
        s.edit_comment(item.key, 999_999, "nope")
    assert s.get(item.key).comments[0].text == "x"  # nothing was written
    assert s.get(item.key).updated_at == before  # and no update was recorded


def test_to_dict_includes_the_comment_id(tmp_path: Path) -> None:
    """Item.to_dict() carries the comment id into JSON, so the API and UI can address it."""
    s = store(tmp_path)
    item = s.create(level="initiative", title="i")
    made = s.comment(item.key, "noah", "hi")
    d = s.get(item.key).to_dict()
    [comment] = d["comments"]
    assert comment["id"] == made.comments[0].id
    assert comment["author"] == "noah" and comment["text"] == "hi"
