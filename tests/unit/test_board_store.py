"""The local board's store: keys, levels, parents, comments, filters, concurrency, export and reopening."""

from __future__ import annotations

import json
import sqlite3
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
    assert (a.key, b.key) == ("AIR-1", "AIR-2") and ISSUE_KEY.fullmatch(a.key)
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
    with pytest.raises(BoardStoreError, match=r"subtask.*story.*AIR-1 is an initiative"):
        s.create(level="subtask", title="s", parent=init.key)
    with pytest.raises(BoardStoreError, match=r"initiative.*no parent"):
        s.create(level="initiative", title="i2", parent=init.key)
    assert [i.key for i in s.list()] == ["AIR-1", "AIR-2"]  # nothing refused was stored


def test_an_item_below_initiative_may_have_no_parent(tmp_path: Path) -> None:
    """A Jira project has epics under no initiative and stories under no epic: they import as they are."""
    s = store(tmp_path)
    epic = s.create(level="epic", title="orphan epic")
    story = s.create(level="story", title="orphan story")
    assert epic.parent is None and story.parent is None
    assert [i.key for i in s.list(level="story")] == [story.key]


def test_a_parent_must_exist(tmp_path: Path) -> None:
    s = store(tmp_path)
    with pytest.raises(BoardStoreError, match=r"AIR-99"):
        s.create(level="epic", title="e", parent="AIR-99")


def test_get_raises_key_error_for_an_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        store(tmp_path).get("AIR-1")


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
        s.update("AIR-99", title="x")
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
        s.comment("AIR-99", "noah", "x")


def test_comment_keeps_a_given_time(tmp_path: Path) -> None:
    """An imported comment keeps the time it was written in Jira, not the time of the import."""
    s = store(tmp_path)
    init = s.create(level="initiative", title="i")
    item = s.comment(init.key, "Noah", "from Jira", at=1_700_000_000.0)
    assert item.comments[0].at == 1_700_000_000.0
    assert item.updated_at >= init.updated_at


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
    assert s.list(parent="AIR-99") == []


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
    s.comment("AIR-1", "noah", "c")
    s.close()
    again = store(tmp_path)
    assert again.get("AIR-1").comments[0].text == "c"
    assert again.create(level="initiative", title="j").key == "AIR-2"
    again.close()


def test_the_store_opens_its_file_on_first_use_not_on_construction(tmp_path: Path) -> None:
    """serve builds the store at startup: a state_dir that is missing or unwritable must not stop serve, and a
    test that builds the app must not touch the person's board."""
    path = tmp_path / "nowhere" / "board.sqlite"
    s = BoardStore(path)
    assert not path.parent.exists()
    s.close()  # nothing to close yet
    assert not path.parent.exists()
    s.create(level="initiative", title="i")
    assert path.exists()
    s.close()
    assert store(tmp_path / "again").list() == []  # a read opens it too


class _CommitFails:
    """A connection whose COMMIT fails (a full disk): sqlite3.Connection's methods cannot be patched."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self.real = real

    def execute(self, sql: str, *args: object) -> object:
        if sql == "COMMIT":
            raise sqlite3.OperationalError("database or disk is full")
        return self.real.execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self.real, name)


def test_a_commit_that_fails_rolls_back_and_the_store_keeps_working(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.create(level="initiative", title="i")
    db = s._db  # noqa: SLF001
    s._conn = _CommitFails(db)  # type: ignore[assignment]  # noqa: SLF001
    with pytest.raises(sqlite3.OperationalError):
        s.create(level="initiative", title="lost")
    s._conn = db  # noqa: SLF001
    assert not db.in_transaction
    assert s.create(level="initiative", title="j").key == "AIR-2"
    assert [i.title for i in s.list()] == ["i", "j"]


def test_create_with_a_number_keeps_it_and_moves_the_counter_past_it(tmp_path: Path) -> None:
    """An import keeps Jira's numbers: HDS-5 stays HDS-5, and the next allocated key comes after it."""
    s = store(tmp_path, prefix="HDS")
    five = s.create(level="initiative", title="five", number=5)
    assert five.key == "HDS-5"
    assert s.create(level="initiative", title="next").key == "HDS-6"
    two = s.create(level="initiative", title="two", number=2)  # below the counter: the counter stays
    assert two.key == "HDS-2"
    assert s.create(level="initiative", title="after").key == "HDS-7"
    assert [i.key for i in s.list()] == ["HDS-2", "HDS-5", "HDS-6", "HDS-7"]  # by number, not insertion


def test_create_refuses_a_number_already_taken_or_not_positive(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.create(level="initiative", title="one")
    with pytest.raises(BoardStoreError, match="AIR-1 is already on the board"):
        s.create(level="initiative", title="again", number=1)
    with pytest.raises(BoardStoreError, match="number"):
        s.create(level="initiative", title="zero", number=0)
    assert [i.key for i in s.list()] == ["AIR-1"]
    assert s.create(level="initiative", title="two").key == "AIR-2"  # nothing refused moved the counter


def test_move_reorders_a_column_and_changes_status(tmp_path: Path) -> None:
    s = store(tmp_path)
    a, b, c = (s.create(level="initiative", title=t).key for t in "abc")
    assert [i.key for i in s.list()] == [a, b, c]
    s.move(c, "todo", a)  # c above a
    assert [i.key for i in s.list(status="todo")] == [c, a, b]
    s.move(c, "todo")  # to the bottom
    assert [i.key for i in s.list(status="todo")] == [a, b, c]
    before = s.get(b).updated_at
    s.move(b, "todo", c)  # same status: not an update
    assert s.get(b).updated_at == before
    moved = s.move(a, "doing")
    assert moved.status == "doing" and moved.updated_at > before
    s.move(c, "doing", a)
    assert [i.key for i in s.list(status="doing")] == [c, a]
    assert [i.key for i in s.list(status="todo")] == [b]


def test_move_refuses_a_bad_target_and_changes_nothing(tmp_path: Path) -> None:
    s = store(tmp_path)
    a = s.create(level="initiative", title="a").key
    b = s.create(level="initiative", title="b").key
    s.move(b, "doing")
    with pytest.raises(BoardStoreError):
        s.move(a, "shipped")  # type: ignore[arg-type]
    with pytest.raises(BoardStoreError):
        s.move(a, "todo", b)  # b is in doing
    with pytest.raises(BoardStoreError):
        s.move(a, "todo", a)  # itself
    with pytest.raises(KeyError):
        s.move("AIR-9", "todo")
    assert s.get(a).status == "todo" and s.get(b).status == "doing"


def test_a_board_from_before_ordering_gains_ranks_in_creation_order(tmp_path: Path) -> None:
    path = tmp_path / "board.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE items (key TEXT PRIMARY KEY, n INTEGER NOT NULL, level TEXT NOT NULL,
        title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL, parent TEXT,
        labels_json TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, created_by TEXT NOT NULL);
        INSERT INTO items VALUES ('AIR-1',1,'initiative','a','','todo',NULL,'[]',1,1,'noah');
        INSERT INTO items VALUES ('AIR-2',2,'initiative','b','','todo',NULL,'[]',1,1,'noah');"""
    )
    conn.commit()
    conn.close()
    s = BoardStore(path)
    assert [i.key for i in s.list()] == ["AIR-1", "AIR-2"]
