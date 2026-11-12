"""The chat thread store: owner-scoped threads and their messages in one SQLite file."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from airlie.chat.threads import StoredMessage, ThreadNotFound, ThreadStore
from tests.support.auth_store import Clock


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "chat" / "threads.db"


@pytest.fixture
def store(db: Path, clock: Clock):
    s = ThreadStore(db, now=clock)
    yield s
    s.close()


def msg(id: str, role: Any = "user", text: str = "hi", created: float = 1.0, **kw: Any) -> StoredMessage:
    return StoredMessage(id, role, [{"type": "text", "text": text}], created, **kw)


def test_a_new_thread_is_created_for_its_owner(store: ThreadStore, clock: Clock) -> None:
    t = store.open("t1", "noah", "plain")

    assert (t.id, t.owner, t.kind, t.title, t.messages, t.todos) == ("t1", "noah", "plain", "", [], [])
    assert (t.created, t.updated) == (clock.t, clock.t)


def test_open_on_an_existing_thread_returns_it_with_its_messages(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("m1", text="hello"))

    again = store.open("t1", "noah", "plain")

    assert [m.id for m in again.messages] == ["m1"]
    assert store.get("t1", "noah").messages == again.messages


def test_another_owners_thread_is_not_found_not_forbidden(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")

    with pytest.raises(ThreadNotFound):
        store.open("t1", "mallory", "plain")
    with pytest.raises(ThreadNotFound):
        store.get("t1", "mallory")
    with pytest.raises(ThreadNotFound):
        store.get("missing", "noah")


def test_opening_a_thread_with_a_different_kind_is_not_found(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")

    with pytest.raises(ThreadNotFound):
        store.open("t1", "noah", "repository")
    assert store.get("t1", "noah").kind == "plain"


def test_append_keeps_order_and_updates_the_updated_time(store: ThreadStore, clock: Clock) -> None:
    store.open("t1", "noah", "plain")
    clock.t += 10
    store.append("t1", msg("b", created=5.0))
    clock.t += 10
    store.append("t1", msg("a", "assistant", created=1.0))

    t = store.get("t1", "noah")

    assert [m.id for m in t.messages] == ["b", "a"]
    assert (t.created, t.updated) == (1_000_000.0, 1_000_020.0)


def test_parts_and_usage_round_trip_exactly(store: ThreadStore) -> None:
    parts: list[dict[str, Any]] = [
        {"type": "reasoning", "text": "think"},
        {
            "type": "tool-web_search",
            "toolCallId": "c1",
            "state": "output-available",
            "input": {"q": "x", "n": [1, 2]},
            "output": {"hits": [{"u": "é"}]},
        },
        {"type": "data-todos", "data": {"items": []}},
        {"type": "text", "text": "done"},
    ]
    usage = {"input_tokens": 3, "output_tokens": 4}
    store.open("t1", "noah", "plain")
    sent = StoredMessage("m1", "assistant", parts, 2.5, usage)
    store.append("t1", sent)

    got = store.get("t1", "noah").messages[0]

    assert got == sent
    assert got.interrupted is False
    assert store.get("t1", "noah").messages[0].usage == usage


def test_an_interrupted_message_is_flagged(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("m1", "assistant", interrupted=True))
    store.append("t1", msg("m2", "assistant"))

    flags = [m.interrupted for m in store.get("t1", "noah").messages]

    assert flags == [True, False]


def test_todos_persist_across_a_reopened_store(db: Path, clock: Clock) -> None:
    todos = [{"id": "1", "text": "a", "status": "done"}, {"id": "2", "text": "b", "status": "in_progress"}]
    first = ThreadStore(db, now=clock)
    first.open("t1", "noah", "plain")
    clock.t += 5
    first.set_todos("t1", todos)
    first.close()

    again = ThreadStore(db, now=clock)
    t = again.get("t1", "noah")
    again.close()

    assert t.todos == todos
    assert t.updated == clock.t


def test_a_bad_stored_todo_row_is_dropped_not_raised(db: Path, store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.set_todos(
        "t1",
        [
            {"id": "1", "text": "ok", "status": "pending"},
            {"id": "2", "status": "pending"},
            {"id": "3", "text": "x", "status": "blocked"},
        ],
    )

    assert store.get("t1", "noah").todos == [{"id": "1", "text": "ok", "status": "pending"}]
    for garbage in ("not json", '{"a": 1}'):
        with sqlite3.connect(db) as c:
            c.execute("update threads set todos = ? where id = 't1'", (garbage,))
        assert store.get("t1", "noah").todos == []


def test_list_is_newest_first_and_filtered_by_owner_and_kind(store: ThreadStore, clock: Clock) -> None:
    store.open("old", "noah", "plain")
    clock.t += 1
    store.open("repo", "noah", "repository")
    clock.t += 1
    store.open("other", "ada", "plain")
    clock.t += 1
    store.open("new", "noah", "plain")
    clock.t += 1
    store.set_todos("old", [])

    assert [s.id for s in store.list("noah")] == ["old", "new", "repo"]
    assert [s.id for s in store.list("noah", "plain")] == ["old", "new"]
    assert [s.id for s in store.list("noah", "repository")] == ["repo"]
    assert [s.id for s in store.list("ada")] == ["other"]
    first = store.list("noah", "repository")[0]
    assert (first.title, first.kind, first.updated) == ("", "repository", 1_000_001.0)


def test_title_is_the_first_user_text_cut_to_60_characters(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("a", "assistant", text="assistant first"))
    store.append("t1", StoredMessage("u0", "user", [{"type": "data-x", "data": 1}], 1.0))
    store.append("t1", msg("u1", text="  " + "x" * 80))
    store.append("t1", msg("u2", text="second"))

    title = store.get("t1", "noah").title

    assert title == "x" * 60
    assert store.list("noah")[0].title == title


def test_title_exactly_60_characters_is_kept_whole(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("u1", text="y" * 60))

    assert store.get("t1", "noah").title == "y" * 60


def test_delete_removes_the_thread_and_its_messages(db: Path, store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("m1"))

    assert store.delete("t1", "noah") is True

    with pytest.raises(ThreadNotFound):
        store.get("t1", "noah")
    with sqlite3.connect(db) as c:
        assert c.execute("select count(*) from messages").fetchone() == (0,)
    assert store.delete("t1", "noah") is False


def test_delete_of_another_owners_thread_is_false_and_leaves_it(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("m1"))

    assert store.delete("t1", "mallory") is False

    assert len(store.get("t1", "noah").messages) == 1
