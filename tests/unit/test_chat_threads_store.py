"""The chat thread store's server-made threads, resumed messages and thread listing."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from airlie.chat.threads import StoredMessage, ThreadNotFound, ThreadStore, ThreadSummary
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


def msg(
    id: str, role: Any = "assistant", parts: list[dict[str, Any]] | None = None, **kw: Any
) -> StoredMessage:
    return StoredMessage(id, role, parts or [{"type": "text", "text": "hi"}], 1.0, **kw)


def test_create_makes_an_empty_thread_with_a_server_chosen_chat_id(store: ThreadStore, clock: Clock) -> None:
    t = store.create("noah", "plain")

    assert re.fullmatch(r"chat-[0-9a-f]{32}", t.id)
    assert (t.owner, t.kind, t.title, t.messages, t.todos) == ("noah", "plain", "", [], [])
    assert (t.repo, t.base_ref, t.base_sha) == (None, None, None)
    assert (t.created, t.updated) == (clock.t, clock.t)
    assert store.get(t.id, "noah") == t


def test_each_created_thread_gets_its_own_id(store: ThreadStore) -> None:
    ids = {store.create("noah", "plain").id for _ in range(3)}

    assert len(ids) == 3
    assert sorted(s.id for s in store.list("noah")) == sorted(ids)


def test_create_keeps_a_repository_chats_repo_and_base_ref(store: ThreadStore) -> None:
    t = store.create("noah", "repository", repo="weyenk/airlie", base_ref="main")

    assert (t.kind, t.repo, t.base_ref, t.base_sha) == ("repository", "weyenk/airlie", "main", None)
    again = store.get(t.id, "noah")
    assert (again.owner, again.kind, again.repo, again.base_ref) == (
        "noah",
        "repository",
        "weyenk/airlie",
        "main",
    )


def test_extend_adds_parts_to_the_end_and_takes_this_turns_usage_and_mark(
    store: ThreadStore, clock: Clock
) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("u1", "user"))
    store.append("t1", msg("a1", parts=[{"type": "text", "text": "one"}], usage={"in": 1}, interrupted=True))
    store.append("t1", msg("a2", parts=[{"type": "text", "text": "two"}]))
    clock.t += 7

    store.extend(
        "t1", "a1", [{"type": "text", "text": "more"}, {"type": "step-start"}], {"in": 2, "out": 3}, False
    )

    t = store.get("t1", "noah")
    assert [m.id for m in t.messages] == ["u1", "a1", "a2"]
    a1 = t.messages[1]
    assert a1.parts == [
        {"type": "text", "text": "one"},
        {"type": "text", "text": "more"},
        {"type": "step-start"},
    ]
    assert (a1.usage, a1.interrupted, a1.created, a1.role) == ({"in": 2, "out": 3}, False, 1.0, "assistant")
    assert t.messages[2] == msg("a2", parts=[{"type": "text", "text": "two"}])
    assert t.messages[0] == msg("u1", "user")
    assert t.updated == clock.t


def test_extend_with_no_usage_clears_it_and_can_mark_the_message_interrupted(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("a1", usage={"in": 1}))

    store.extend("t1", "a1", [], None, True)

    [a1] = store.get("t1", "noah").messages
    assert (a1.parts, a1.usage, a1.interrupted) == ([{"type": "text", "text": "hi"}], None, True)


def test_extend_with_no_usage_stores_sql_null_not_json_null(db: Path, store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("a1", usage={"in": 1}))

    store.extend("t1", "a1", [], None, True)

    with sqlite3.connect(db) as c:
        assert c.execute("select usage, interrupted from messages where id = 'a1'").fetchone() == (None, 1)


def test_extend_touches_only_the_message_of_that_thread(store: ThreadStore, clock: Clock) -> None:
    store.open("t1", "noah", "plain")
    store.open("t2", "noah", "plain")
    store.append("t1", msg("a1"))
    store.append("t2", msg("a1"))
    before = store.get("t2", "noah")
    clock.t += 5

    store.extend("t2", "a1", [{"type": "text", "text": "x"}], {"n": 1}, True)

    assert store.get("t1", "noah").messages == [msg("a1")]
    assert store.get("t1", "noah").updated == before.updated
    assert store.get("t2", "noah").messages == [
        msg(
            "a1",
            parts=[{"type": "text", "text": "hi"}, {"type": "text", "text": "x"}],
            usage={"n": 1},
            interrupted=True,
        )
    ]
    assert store.get("t2", "noah").updated == clock.t


def test_extend_of_a_message_not_in_the_thread_is_not_found_and_changes_nothing(
    store: ThreadStore, clock: Clock
) -> None:
    store.open("t1", "noah", "plain")
    store.open("t2", "noah", "plain")
    store.append("t2", msg("a1"))
    clock.t += 5

    with pytest.raises(ThreadNotFound) as err:
        store.extend("t1", "a1", [{"type": "text", "text": "x"}], None, False)
    with pytest.raises(ThreadNotFound, match=r"^t2: no message a9$"):
        store.extend("t2", "a9", [], None, False)

    assert str(err.value) == "t1: no message a1"
    assert store.get("t2", "noah").messages == [msg("a1")]
    assert store.get("t2", "noah").updated == clock.t - 5


def test_list_summarises_every_field_with_created_apart_from_updated(
    store: ThreadStore, clock: Clock
) -> None:
    store.open("r1", "noah", "repository", repo="weyenk/airlie", base_ref="dev")
    created = clock.t
    clock.t += 3
    store.append("r1", msg("m1", "user", [{"type": "text", "text": "fix it"}]))

    assert store.list("noah") == [
        ThreadSummary("r1", "fix it", "repository", created + 3, created, "weyenk/airlie", "dev")
    ]
    assert store.list("noah", "repository") == store.list("noah")


def test_list_breaks_a_tie_in_updated_time_newest_made_first(store: ThreadStore) -> None:
    for i in ("a", "b", "c"):
        store.open(i, "noah", "plain")

    assert [s.id for s in store.list("noah")] == ["c", "b", "a"]
    assert [s.id for s in store.list("noah", "plain")] == ["c", "b", "a"]


def test_list_by_kind_keeps_to_the_owner(store: ThreadStore) -> None:
    store.open("mine", "noah", "plain")
    store.open("theirs", "ada", "plain")
    store.open("repo", "ada", "repository")

    assert [s.id for s in store.list("noah", "plain")] == ["mine"]
    assert store.list("noah", "repository") == []
    assert [s.id for s in store.list("ada", "plain")] == ["theirs"]


def test_list_puts_the_latest_updated_thread_first_whatever_order_they_were_made(
    store: ThreadStore, clock: Clock
) -> None:
    store.open("a", "noah", "plain")
    store.open("b", "noah", "plain")
    clock.t += 1
    store.append("a", msg("m1", "user"))

    assert [s.id for s in store.list("noah")] == ["a", "b"]
    assert [s.id for s in store.list("noah", "plain")] == ["a", "b"]
