"""The chat thread store's failed approved calls and retried turns."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from airlie.chat.threads import StoredMessage, ThreadStore
from tests.support.auth_store import Clock


@pytest.fixture
def store(tmp_path: Path):
    s = ThreadStore(tmp_path / "chat" / "threads.db", now=Clock())
    yield s
    s.close()


def msg(id: str, role: Any = "assistant", parts: list[dict[str, Any]] | None = None) -> StoredMessage:
    return StoredMessage(id, role, parts or [{"type": "text", "text": id}], 1.0)


def prompt_part(approval_id: str) -> dict[str, Any]:
    return {"type": "data-approval", "id": approval_id, "data": {"tool": "run", "answer": "approved"}}


def parts_of(store: ThreadStore, thread_id: str) -> dict[str, list[dict[str, Any]]]:
    return {m.id: m.parts for m in store.get(thread_id, "noah").messages}


def test_fail_answer_marks_the_named_prompt_failed_with_the_calls_error_and_keeps_its_other_data(
    store: ThreadStore,
) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("u1", "user"))
    store.append("t1", msg("a1", parts=[{"type": "text", "text": "running"}, prompt_part("p1")]))

    store.fail_answer("t1", "p1", "exit 2")

    assert parts_of(store, "t1") == {
        "u1": [{"type": "text", "text": "u1"}],
        "a1": [
            {"type": "text", "text": "running"},
            {
                "type": "data-approval",
                "id": "p1",
                "data": {"tool": "run", "answer": "failed", "error": "exit 2"},
            },
        ],
    }


def test_fail_answer_finds_the_prompt_in_whichever_assistant_message_holds_it(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("a1", parts=[prompt_part("p1")]))
    store.append("t1", msg("a2", parts=[prompt_part("p2")]))
    store.append("t1", msg("a3"))

    store.fail_answer("t1", "p2", "boom")

    got = parts_of(store, "t1")
    assert got["a1"] == [prompt_part("p1")]
    assert got["a2"][0]["data"] == {"tool": "run", "answer": "failed", "error": "boom"}
    assert got["a3"] == [{"type": "text", "text": "a3"}]


def test_fail_answer_leaves_other_requests_other_part_types_user_messages_and_other_threads_alone(
    store: ThreadStore,
) -> None:
    store.open("t1", "noah", "plain")
    store.open("t2", "noah", "plain")
    other_type = {"type": "data-note", "id": "p1", "data": {"text": "approval pending"}}
    store.append("t1", msg("u1", "user", [prompt_part("p1")]))
    store.append("t1", msg("a1", parts=[prompt_part("p9"), other_type, prompt_part("p1")]))
    store.append("t2", msg("b1", parts=[prompt_part("p1")]))

    store.fail_answer("t1", "p1", "denied by sandbox")

    got = parts_of(store, "t1")
    assert got["u1"] == [prompt_part("p1")]
    assert got["a1"][:2] == [prompt_part("p9"), other_type]
    assert got["a1"][2]["data"]["answer"] == "failed"
    assert parts_of(store, "t2") == {"b1": [prompt_part("p1")]}


def test_fail_answer_for_an_unknown_request_changes_nothing(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("a1", parts=[prompt_part("p1")]))

    store.fail_answer("t1", "nope", "boom")

    assert parts_of(store, "t1") == {"a1": [prompt_part("p1")]}


def test_drop_after_deletes_every_message_saved_after_the_named_one_in_that_thread_only(
    store: ThreadStore,
) -> None:
    store.open("t1", "noah", "plain")
    store.open("t2", "noah", "plain")
    store.append("t1", msg("u1", "user"))
    store.append("t2", msg("v1", "user"))
    store.append("t1", msg("a1"))
    store.append("t1", msg("u2", "user"))
    store.append("t2", msg("w1"))
    store.append("t1", msg("a2"))

    store.drop_after("t1", "a1")

    assert [m.id for m in store.get("t1", "noah").messages] == ["u1", "a1"]
    assert [m.id for m in store.get("t2", "noah").messages] == ["v1", "w1"]


def test_drop_after_the_last_message_keeps_everything(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", msg("u1", "user"))
    store.append("t1", msg("a1"))

    store.drop_after("t1", "a1")

    assert [m.id for m in store.get("t1", "noah").messages] == ["u1", "a1"]


def test_drop_after_a_message_the_thread_does_not_have_keeps_everything(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.open("t2", "noah", "plain")
    store.append("t2", msg("x1"))
    store.append("t1", msg("u1", "user"))
    store.append("t1", msg("a1"))

    store.drop_after("t1", "missing")
    store.drop_after("t1", "x1")

    assert [m.id for m in store.get("t1", "noah").messages] == ["u1", "a1"]
