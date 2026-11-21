"""OpenAI Chat Completions <-> Anthropic Messages translation (airlie.engine.anthropic)."""

from __future__ import annotations

import json

import pytest

from airlie.engine.anthropic import (
    from_anthropic_message_response,
    iter_openai_chunks_from_anthropic_sse,
    to_anthropic_messages_body,
)
from airlie.engine.dialect import dialect_for, thinking_fields


def test_to_anthropic_moves_system_and_maps_messages():
    body = {
        "model": "smart",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ],
        "max_tokens": 64,
    }
    out = to_anthropic_messages_body(body)
    assert out["model"] == "smart"
    assert out["system"] == "You are helpful."
    assert out["messages"] == [{"role": "user", "content": "Hi"}]
    assert out["max_tokens"] == 64


def test_from_anthropic_response_to_chat_completion():
    raw = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    cc = from_anthropic_message_response(raw)
    assert cc["choices"][0]["message"]["content"] == "Hello"
    assert cc["usage"]["prompt_tokens"] == 3


def test_request_defaults_max_tokens_and_maps_sampling_and_stop():
    out = to_anthropic_messages_body(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 5,
            "stop": "END",
            "stream": True,
            "presence_penalty": 0.5,
            "chat_template_kwargs": {"enable_thinking": True},
        }
    )
    assert out["max_tokens"] > 0
    assert out["temperature"] == 0.2
    assert out["top_p"] == 0.9
    assert out["top_k"] == 5
    assert out["stop_sequences"] == ["END"]
    assert out["stream"] is True
    assert "presence_penalty" not in out
    assert "chat_template_kwargs" not in out


def test_max_completion_tokens_is_accepted():
    out = to_anthropic_messages_body(
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "max_completion_tokens": 7}
    )
    assert out["max_tokens"] == 7


def test_multiple_system_messages_are_joined_and_blocks_flattened():
    out = to_anthropic_messages_body(
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": "a"},
                {"role": "system", "content": [{"type": "text", "text": "b"}]},
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            ],
        }
    )
    assert out["system"] == "a\n\nb"
    assert out["messages"] == [{"role": "user", "content": "hi"}]


def test_tools_and_tool_choice_map_to_anthropic_shape():
    tool = {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    out = to_anthropic_messages_body(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [tool],
            "tool_choice": "required",
        }
    )
    assert out["tools"] == [
        {"name": "bash", "description": "run", "input_schema": {"type": "object", "properties": {}}}
    ]
    assert out["tool_choice"] == {"type": "any"}
    named = to_anthropic_messages_body(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": "bash"}},
        }
    )
    assert named["tool_choice"] == {"type": "tool", "name": "bash"}
    none = to_anthropic_messages_body(
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": [tool], "tool_choice": "none"}
    )
    assert "tools" not in none
    assert "tool_choice" not in none


def test_assistant_tool_calls_and_tool_results_round_trip_into_blocks():
    out = to_anthropic_messages_body(
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "run it"},
                {
                    "role": "assistant",
                    "content": "ok",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"cmd": "ls"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
                {"role": "tool", "tool_call_id": "c2", "content": "b.txt"},
                {"role": "user", "content": "thanks"},
            ],
        }
    )
    assert out["messages"] == [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "c1", "name": "bash", "input": {"cmd": "ls"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "a.txt"},
                {"type": "tool_result", "tool_use_id": "c2", "content": "b.txt"},
                {"type": "text", "text": "thanks"},
            ],
        },
    ]


def test_consecutive_same_role_messages_are_merged():
    out = to_anthropic_messages_body(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
        }
    )
    assert out["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    ]


def test_image_url_data_uri_becomes_base64_source():
    out = to_anthropic_messages_body(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
                }
            ],
        }
    )
    assert out["messages"][0]["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    ]


def test_response_tool_use_and_stop_reasons():
    raw = {
        "id": "msg_2",
        "model": "smart",
        "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "calling"},
            {"type": "tool_use", "id": "tu1", "name": "bash", "input": {"cmd": "ls"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 7},
    }
    cc = from_anthropic_message_response(raw)
    choice = cc["choices"][0]
    assert cc["object"] == "chat.completion"
    assert cc["model"] == "smart"
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "calling"
    assert choice["message"]["reasoning_content"] == "hmm"
    [call] = choice["message"]["tool_calls"]
    assert call["id"] == "tu1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "bash"
    assert json.loads(call["function"]["arguments"]) == {"cmd": "ls"}
    assert cc["usage"] == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}


@pytest.mark.parametrize(
    ("stop", "finish"),
    [("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"), ("tool_use", "tool_calls")],
)
def test_stop_reason_mapping(stop, finish):
    cc = from_anthropic_message_response(
        {"content": [{"type": "text", "text": "x"}], "stop_reason": stop, "usage": {}}
    )
    assert cc["choices"][0]["finish_reason"] == finish


def _sse(*events: dict) -> list[str]:
    lines: list[str] = []
    for data in events:
        lines += [f"event: {data['type']}", f"data: {json.dumps(data)}", ""]
    return lines


def _start(input_tokens: int = 1) -> dict:
    return {
        "type": "message_start",
        "message": {"id": "msg_1", "model": "smart", "usage": {"input_tokens": input_tokens}},
    }


def _block(index: int, block: dict) -> dict:
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(index: int, delta: dict) -> dict:
    return {"type": "content_block_delta", "index": index, "delta": delta}


def _stop(index: int) -> dict:
    return {"type": "content_block_stop", "index": index}


def _end(reason: str, output_tokens: int) -> list[dict]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": reason},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]


def test_stream_text_deltas_become_chat_chunks():
    lines = _sse(
        _start(4),
        _block(0, {"type": "text", "text": ""}),
        {"type": "ping"},
        _delta(0, {"type": "text_delta", "text": "Hel"}),
        _delta(0, {"type": "text_delta", "text": "lo"}),
        _stop(0),
        *_end("end_turn", 2),
    )
    chunks = list(iter_openai_chunks_from_anthropic_sse(lines))
    assert all(c["object"] == "chat.completion.chunk" and c["id"] == "msg_1" for c in chunks)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "Hello"
    finals = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
    assert [c["choices"][0]["finish_reason"] for c in finals] == ["stop"]
    assert chunks[-1]["usage"] == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}


def test_stream_tool_use_and_thinking_deltas():
    lines = _sse(
        _start(),
        _block(0, {"type": "thinking", "thinking": ""}),
        _delta(0, {"type": "thinking_delta", "thinking": "hm"}),
        _stop(0),
        _block(1, {"type": "tool_use", "id": "tu", "name": "bash", "input": {}}),
        _delta(1, {"type": "input_json_delta", "partial_json": '{"cmd"'}),
        _delta(1, {"type": "input_json_delta", "partial_json": ': "ls"}'}),
        _stop(1),
        *_end("tool_use", 9),
    )
    chunks = [c for c in iter_openai_chunks_from_anthropic_sse(lines) if c["choices"]]
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert "".join(d.get("reasoning_content", "") for d in deltas) == "hm"
    calls = [tc for d in deltas for tc in d.get("tool_calls", [])]
    assert calls[0]["index"] == 0
    assert calls[0]["id"] == "tu"
    assert calls[0]["function"]["name"] == "bash"
    assert "".join(tc["function"].get("arguments", "") for tc in calls) == '{"cmd": "ls"}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_error_event_raises():
    lines = _sse({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    with pytest.raises(RuntimeError, match="busy"):
        list(iter_openai_chunks_from_anthropic_sse(lines))


def test_dialect_is_the_engine_format():
    assert dialect_for("openai") == "openai"
    assert dialect_for("anthropic") == "anthropic"
    with pytest.raises(ValueError):
        dialect_for("mlx_vlm")


def test_thinking_fields_are_openai_compatible_kwargs_only():
    assert thinking_fields(
        dialect="openai",
        thinking=True,
        budget=4096,
        chat_template_kwargs={"reasoning_strength": "low"},
        tool_parser="atem",
        tools=True,
    ) == {"chat_template_kwargs": {"enable_thinking": True, "reasoning_strength": "low"}}
