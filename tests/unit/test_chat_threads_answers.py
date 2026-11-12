"""Answering a chat's permission requests: `mark_answered` on a message's parts and `ThreadStore.answer` on
the stored messages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from airlie.chat.threads import StoredMessage, ThreadStore, mark_answered
from tests.support.auth_store import Clock


@pytest.fixture
def store(tmp_path: Path):
    s = ThreadStore(tmp_path / "chat" / "threads.db", now=Clock())
    yield s
    s.close()


def tool_part(approval_id: str, state: str = "approval-requested") -> dict[str, Any]:
    return {"type": "tool-run", "toolCallId": "c1", "state": state, "approval": {"id": approval_id}}


def prompt_part(approval_id: str) -> dict[str, Any]:
    return {"type": "data-approval", "id": approval_id, "data": {"tool": "run"}}


def test_a_yes_marks_the_tool_part_responded_and_approved_and_the_prompt_approved() -> None:
    parts = [tool_part("a1"), prompt_part("a1")]

    assert mark_answered(parts, {"a1": True}) is True

    assert parts == [
        {
            "type": "tool-run",
            "toolCallId": "c1",
            "state": "approval-responded",
            "approval": {"id": "a1", "approved": True},
        },
        {"type": "data-approval", "id": "a1", "data": {"tool": "run", "answer": "approved"}},
    ]


def test_a_no_marks_the_tool_part_responded_not_approved_and_the_prompt_denied() -> None:
    parts = [tool_part("a1"), prompt_part("a1")]

    assert mark_answered(parts, {"a1": False}) is True

    assert parts == [
        {
            "type": "tool-run",
            "toolCallId": "c1",
            "state": "approval-responded",
            "approval": {"id": "a1", "approved": False},
        },
        {"type": "data-approval", "id": "a1", "data": {"tool": "run", "answer": "denied"}},
    ]


def test_each_request_takes_its_own_answer() -> None:
    parts = [tool_part("a1"), prompt_part("a1"), tool_part("a2"), prompt_part("a2")]

    mark_answered(parts, {"a1": False, "a2": True})

    assert [parts[0]["approval"], parts[2]["approval"]] == [
        {"id": "a1", "approved": False},
        {"id": "a2", "approved": True},
    ]
    assert [parts[1]["data"]["answer"], parts[3]["data"]["answer"]] == ["denied", "approved"]


def test_a_lone_tool_part_answered_is_a_change() -> None:
    parts = [tool_part("a1")]

    assert mark_answered(parts, {"a1": True}) is True
    assert parts[0]["state"] == "approval-responded"


def test_a_lone_prompt_part_answered_is_a_change() -> None:
    parts = [prompt_part("a1")]

    assert mark_answered(parts, {"a1": True}) is True
    assert parts[0]["data"] == {"tool": "run", "answer": "approved"}


def test_requests_not_named_in_the_answers_and_other_parts_are_left_alone_and_nothing_changed() -> None:
    parts = [
        {"type": "text", "text": "hi"},
        tool_part("other"),
        prompt_part("other"),
        {"type": "tool-read", "id": "a1", "state": "output-available"},
        {"type": "tool-run", "approval": "a1", "state": "approval-requested"},
    ]
    before = [dict(p) for p in parts]

    assert mark_answered(parts, {"a1": True}) is False

    assert parts == before


def test_no_parts_is_no_change() -> None:
    assert mark_answered([], {"a1": True}) is False


def assistant(id: str, parts: list[dict[str, Any]]) -> StoredMessage:
    return StoredMessage(id, "assistant", parts, 1.0)


def stored_parts(store: ThreadStore, thread_id: str = "t1") -> dict[str, list[dict[str, Any]]]:
    return {m.id: m.parts for m in store.get(thread_id, "noah").messages}


def test_answer_saves_the_marked_parts_so_a_reload_shows_the_prompt_answered(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", StoredMessage("u1", "user", [{"type": "text", "text": "run it"}], 1.0))
    asking = [{"type": "text", "text": "may I?"}, tool_part("a1"), prompt_part("a1")]
    store.append("t1", assistant("m1", asking))
    store.append("t1", assistant("m2", [tool_part("a2"), prompt_part("a2")]))

    store.answer("t1", {"a1": True, "a2": False})

    parts = stored_parts(store)
    assert parts["m1"] == [
        {"type": "text", "text": "may I?"},
        {
            "type": "tool-run",
            "toolCallId": "c1",
            "state": "approval-responded",
            "approval": {"id": "a1", "approved": True},
        },
        {"type": "data-approval", "id": "a1", "data": {"tool": "run", "answer": "approved"}},
    ]
    assert parts["m2"][0]["approval"] == {"id": "a2", "approved": False}
    assert parts["m2"][1]["data"]["answer"] == "denied"
    assert parts["u1"] == [{"type": "text", "text": "run it"}]


def test_answer_leaves_other_threads_user_messages_and_unnamed_requests_untouched(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.open("t2", "noah", "plain")
    store.append("t1", assistant("m1", [tool_part("a9"), prompt_part("a9")]))
    store.append("t1", StoredMessage("u1", "user", [tool_part("a1"), prompt_part("a1")], 1.0))
    store.append("t2", assistant("m2", [tool_part("a1"), prompt_part("a1")]))

    store.answer("t1", {"a1": True})

    assert stored_parts(store, "t1") == {
        "m1": [tool_part("a9"), prompt_part("a9")],
        "u1": [tool_part("a1"), prompt_part("a1")],
    }
    assert stored_parts(store, "t2") == {"m2": [tool_part("a1"), prompt_part("a1")]}


def test_answer_with_no_answers_changes_nothing(store: ThreadStore) -> None:
    store.open("t1", "noah", "plain")
    store.append("t1", assistant("m1", [tool_part("a1"), prompt_part("a1")]))

    store.answer("t1", {})

    assert stored_parts(store) == {"m1": [tool_part("a1"), prompt_part("a1")]}
