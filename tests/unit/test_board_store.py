"""The local board's store: keys, levels, parents, comments, filters, concurrency, export and reopening."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from airlie.board import LEVELS, BoardStore, BoardStoreError, Item, parent_level
from airlie.story.model import ISSUE_KEY


def store(tmp_path: Path, **kw: object) -> BoardStore:
    return BoardStore(tmp_path / "board.sqlite", **kw)  # type: ignore[arg-type]


def test_levels_and_parent_level() -> None:
    assert LEVELS == ("initiative", "epic", "story", "subtask")
    assert parent_level("initiative") is None
    assert parent_level("epic") == "initiative"
    assert parent_level("subtask") == "story"


def test_create_allocates_keys_with_the_prefix_and_sets_defaults(tmp_path: Path) -> None:
    s = store(tmp_path)
    a = s.create(level="initiative", title="Remote studio")
    b = s.create(level="initiative", title="Board", description="d", labels=["repo:weyenk/airlie"])
    assert (a.key, b.key) == ("AL-1", "AL-2") and ISSUE_KEY.fullmatch(a.key)
    assert a.status == "todo" and a.labels == () and a.created_by == "noah" and a.parent is None
    assert a.description == "" and a.comments == () and a.created_at == a.updated_at > 0
    assert b.labels == ("repo:weyenk/airlie",) and b.description == "d"
    assert isinstance(a, Item)
    with_prefix = store(tmp_path / "other", prefix="HDS")
    assert with_prefix.create(level="initiative", title="x").key == "HDS-1"


def test_a_parent_must_be_exactly_one_level_up(tmp_path: Path) -> None:
    s = store(tmp_path)
    init = s.create(level="initiative", title="i")
    epic = s.create(level="epic", title="e", parent=init.key)
    assert epic.parent == init.key
    with pytest.raises(BoardStoreError, match=r"subtask.*story.*AL-1 is an initiative"):
        s.create(level="subtask", title="s", parent=init.key)
    with pytest.raises(BoardStoreError, match=r"initiative.*no parent"):
        s.create(level="initiative", title="i2", parent=init.key)
    with pytest.raises(BoardStoreError, match=r"epic.*needs a parent"):
        s.create(level="epic", title="orphan")
    assert [i.key for i in s.list()] == ["AL-1", "AL-2"]  # nothing refused was stored


def test_a_parent_must_exist(tmp_path: Path) -> None:
    s = store(tmp_path)
    with pytest.raises(BoardStoreError, match=r"AL-99"):
        s.create(level="epic", title="e", parent="AL-99")


def test_get_raises_key_error_for_an_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        store(tmp_path).get("AL-1")


def test_update_changes_fields_and_bumps_updated_at(tmp_path: Path) -> None:
    s = store(tmp_path)
    init = s.create(level="initiative", title="i")
    epic = s.create(level="epic", title="e", parent=init.key)
    story = s.create(level="story", title="s", parent=epic.key)
    done = s.update(story.key, title="S", description="desc", status="doing", labels=["a", "b"])
    assert (done.title, done.description, done.status, done.labels) == ("S", "desc", "doing", ("a", "b"))
    assert done.updated_at >= story.updated_at and done.created_at == story.created_at
    assert s.get(story.key) == done
    other_epic = s.create(level="epic", title="e2", parent=init.key)
    assert s.update(story.key, parent=other_epic.key).parent == other_epic.key
    with pytest.raises(BoardStoreError):
        s.update(story.key, parent=init.key)
    with pytest.raises(BoardStoreError, match="status"):
        s.update(story.key, status="shipped")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        s.update("AL-99", title="x")
    assert s.get(story.key).parent == other_epic.key  # a refused update changes nothing


def test_comment_appends_in_order_with_author(tmp_path: Path) -> None:
    s = store(tmp_path)
    init = s.create(level="initiative", title="i")
    s.comment(init.key, "noah", "first")
    item = s.comment(init.key, "airlie", "second")
    assert [(c.author, c.text) for c in item.comments] == [("noah", "first"), ("airlie", "second")]
    assert item.comments[0].at <= item.comments[1].at
    assert s.get(init.key).comments == item.comments
    with pytest.raises(KeyError):
        s.comment("AL-99", "noah", "x")


def test_list_filters_by_level_status_and_parent_in_created_order(tmp_path: Path) -> None:
    s = store(tmp_path)
    init = s.create(level="initiative", title="i")
    e1 = s.create(level="epic", title="e1", parent=init.key)
    e2 = s.create(level="epic", title="e2", parent=init.key)
    s1 = s.create(level="story", title="s1", parent=e1.key)
    s2 = s.create(level="story", title="s2", parent=e2.key)
    s.update(s1.key, status="doing")
    assert [i.key for i in s.list()] == [init.key, e1.key, e2.key, s1.key, s2.key]
    assert [i.key for i in s.list(level="epic")] == [e1.key, e2.key]
    assert [i.key for i in s.list(level="story", status="todo")] == [s2.key]
    assert [i.key for i in s.list(parent=e1.key)] == [s1.key]
    assert s.list(parent="AL-99") == []


def test_keys_are_unique_under_concurrent_creates(tmp_path: Path) -> None:
    s = store(tmp_path)
    keys: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        for _ in range(5):
            k = s.create(level="initiative", title="t").key
            with lock:
                keys.append(k)

    threads = [threading.Thread(target=work) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(keys) == 100 and len(set(keys)) == 100
    assert {int(k.split("-")[1]) for k in keys} == set(range(1, 101))


def test_export_round_trips_every_field(tmp_path: Path) -> None:
    s = store(tmp_path, prefix="HDS")
    init = s.create(level="initiative", title="i", description="d", labels=["x"], created_by="bot")
    s.comment(init.key, "noah", "hi")
    out = s.export()
    assert out["prefix"] == "HDS"
    assert out["items"] == [s.get(init.key).to_dict()]
    item = json.loads(json.dumps(out))["items"][0]  # JSON-serialisable
    assert item == {
        "key": "HDS-1",
        "level": "initiative",
        "title": "i",
        "description": "d",
        "status": "todo",
        "parent": None,
        "labels": ["x"],
        "comments": [{"author": "noah", "text": "hi", "at": item["comments"][0]["at"]}],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "created_by": "bot",
    }


def test_the_store_survives_reopening(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.create(level="initiative", title="i")
    s.comment("AL-1", "noah", "c")
    s.close()
    again = store(tmp_path)
    assert again.get("AL-1").comments[0].text == "c"
    assert again.create(level="initiative", title="j").key == "AL-2"
    again.close()
