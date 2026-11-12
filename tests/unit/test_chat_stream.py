"""The AI SDK UI message stream encoder."""

import json
from typing import Any

from airlie.chat.stream import DONE, STREAM_HEADERS, StreamWriter, sse


def chunks(events: list[str]) -> list[dict[str, Any]]:
    return [json.loads(e[6:]) for e in events if e != DONE]


def test_start_emits_start_then_start_step_with_the_message_id() -> None:
    assert chunks(StreamWriter("m1").start()) == [
        {"type": "start", "messageId": "m1"},
        {"type": "start-step"},
    ]


def test_headers_name_the_protocol_version() -> None:
    assert STREAM_HEADERS == {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        "x-vercel-ai-ui-message-stream": "v1",
        "x-accel-buffering": "no",
    }


def test_first_text_delta_opens_a_part_and_later_ones_reuse_its_id() -> None:
    w = StreamWriter("m")
    first = chunks(w.text("a"))
    second = chunks(w.text("b"))
    assert [c["type"] for c in first] == ["text-start", "text-delta"]
    assert [c["type"] for c in second] == ["text-delta"]
    assert first[0]["id"] == first[1]["id"] == second[0]["id"]
    assert (first[1]["delta"], second[0]["delta"]) == ("a", "b")


def test_switching_from_reasoning_to_text_closes_the_reasoning_part() -> None:
    w = StreamWriter("m")
    r = chunks(w.reasoning("think"))
    t = chunks(w.text("say"))
    assert [c["type"] for c in r] == ["reasoning-start", "reasoning-delta"]
    assert [c["type"] for c in t] == ["reasoning-end", "text-start", "text-delta"]
    assert t[0]["id"] == r[0]["id"]
    assert t[1]["id"] != r[0]["id"]


def test_a_new_text_part_after_a_close_gets_a_fresh_id() -> None:
    w = StreamWriter("m")
    a = chunks(w.text("x"))[0]["id"]
    w.tool_call("c", "t", "{}")
    b = chunks(w.text("y"))[0]["id"]
    assert a != b


def test_a_tool_call_closes_an_open_text_part_then_announces_input() -> None:
    w = StreamWriter("m")
    opened = chunks(w.text("hi"))[0]["id"]
    out = chunks(w.tool_call("c1", "read_file", '{"path": "a"}'))
    assert [c["type"] for c in out] == ["text-end", "tool-input-start", "tool-input-available"]
    assert out[0]["id"] == opened
    assert out[1] == {"type": "tool-input-start", "toolCallId": "c1", "toolName": "read_file"}


def test_tool_input_available_carries_the_parsed_arguments() -> None:
    out = chunks(StreamWriter("m").tool_call("c1", "read_file", '{"path": "a", "n": 2}'))
    assert out[-1] == {
        "type": "tool-input-available",
        "toolCallId": "c1",
        "toolName": "read_file",
        "input": {"path": "a", "n": 2},
    }


def test_unparseable_arguments_are_sent_raw_not_dropped() -> None:
    out = chunks(StreamWriter("m").tool_call("c1", "t", '{"path": '))
    assert out[-1]["input"] == {"_raw": '{"path": '}


def test_a_tool_result_is_tool_output_available() -> None:
    assert chunks(StreamWriter("m").tool_result("c1", "done", True)) == [
        {"type": "tool-output-available", "toolCallId": "c1", "output": "done"}
    ]


def test_a_failed_tool_result_is_tool_output_error_with_its_text() -> None:
    assert chunks(StreamWriter("m").tool_result("c1", "boom", False)) == [
        {"type": "tool-output-error", "toolCallId": "c1", "errorText": "boom"}
    ]


def test_data_parts_are_named_and_may_be_transient() -> None:
    w = StreamWriter("m")
    assert chunks(w.data("todo", {"a": 1})) == [{"type": "data-todo", "data": {"a": 1}}]
    assert chunks(w.data("note", "x", transient=True)) == [
        {"type": "data-note", "data": "x", "transient": True}
    ]


def test_next_step_finishes_one_step_and_starts_another() -> None:
    assert chunks(StreamWriter("m").next_step()) == [{"type": "finish-step"}, {"type": "start-step"}]


def test_next_step_closes_an_open_part_first() -> None:
    w = StreamWriter("m")
    w.text("x")
    assert [c["type"] for c in chunks(w.next_step())] == ["text-end", "finish-step", "start-step"]


def test_finish_closes_the_open_part_carries_usage_and_ends_with_done() -> None:
    w = StreamWriter("m")
    opened = chunks(w.reasoning("r"))[0]["id"]
    out = w.finish({"input": 3, "output": 4})
    assert out[-1] == DONE
    assert chunks(out) == [
        {"type": "reasoning-end", "id": opened},
        {"type": "finish-step"},
        {"type": "finish", "messageMetadata": {"usage": {"input": 3, "output": 4}}},
    ]


def test_finish_with_nothing_open_has_no_end_chunk() -> None:
    types = [c["type"] for c in chunks(StreamWriter("m").finish({}))]
    assert types == ["finish-step", "finish"]


def test_error_chunk_carries_the_text() -> None:
    assert chunks(StreamWriter("m").error("bad")) == [{"type": "error", "errorText": "bad"}]


def test_a_delta_with_a_newline_stays_on_one_sse_line() -> None:
    event = StreamWriter("m").text("a\nb\r\nc")[1]
    assert event.count("\n") == 2
    assert json.loads(event[6:])["delta"] == "a\nb\r\nc"


def test_each_event_ends_with_a_blank_line() -> None:
    w = StreamWriter("m")
    events = w.start() + w.text("x") + w.finish({})
    assert all(e.endswith("\n\n") and e.startswith("data: ") for e in events)


def test_sse_is_compact_json() -> None:
    assert sse({"a": 1, "b": [1, 2]}) == 'data: {"a":1,"b":[1,2]}\n\n'


def test_unparseable_tool_arguments_are_an_input_error_chunk() -> None:
    w = StreamWriter("m")
    w.text("a")

    assert chunks(w.tool_input_error("c1", "todo", '{"items": [', "arguments are not JSON")) == [
        {"type": "text-end", "id": "m-text-1"},
        {"type": "tool-input-start", "toolCallId": "c1", "toolName": "todo"},
        {
            "type": "tool-input-error",
            "toolCallId": "c1",
            "toolName": "todo",
            "input": '{"items": [',
            "errorText": "arguments are not JSON",
        },
    ]


def test_a_reasoning_delta_carries_its_text_and_the_part_id() -> None:
    w = StreamWriter("m")
    first = chunks(w.reasoning("think "))
    second = chunks(w.reasoning("more"))

    assert (first[1]["delta"], second[0]["delta"]) == ("think ", "more")
    assert first[0]["id"] == first[1]["id"] == second[0]["id"]
