"""HTTPEngine over httpx.MockTransport: an OpenAI Chat Completions server (recorded fixtures) and an
Anthropic Messages one (events built here). Both reach the same Engine seam."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.config import RoleSpec
from airlie.engine import EngineError, Message, TokenEvent, ToolSchema
from airlie.engine.http_engine import HTTPEngine
from tests.unit.engine_contract import check_plain_stream, plain_request

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "openai_http"

OPENAI_CHAT = "/v1/chat/completions"
ANTHROPIC_MESSAGES = "/v1/messages"


def role_spec(prompt: Path = Path("/dev/null"), **kw: Any) -> RoleSpec:
    base: dict[str, Any] = {
        "prompt": prompt,
        "model": "small",
        "thinking": False,
        "thinking_budget": None,
        "max_tokens": 64,
    }
    return RoleSpec(**{**base, **kw})


class Recorder:
    """MockTransport handler that serves canned bytes and records requests."""

    def __init__(self, routes: dict[str, bytes | tuple[int, bytes]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path
        if key in (OPENAI_CHAT, ANTHROPIC_MESSAGES):
            body = json.loads(request.content)
            key = "stream" if body.get("stream") else "nonstream"
        resp = self.routes[key]
        status, content = resp if isinstance(resp, tuple) else (200, resp)
        stream = content.startswith((b"data:", b"event:"))
        headers = {"content-type": "text/event-stream" if stream else "application/json"}
        return httpx.Response(status, content=content, headers=headers)


def engine(
    routes: dict[str, bytes | tuple[int, bytes]],
    *,
    spec: RoleSpec | None = None,
    base_url: str = "http://engine.test/v1",
    **kw: Any,
) -> tuple[HTTPEngine, Recorder]:
    rec = Recorder(routes)
    eng = HTTPEngine(
        base_url=base_url,
        passthru_name_for_role=lambda role: "small",
        role_spec_for=lambda role: spec or role_spec(),
        transport=httpx.MockTransport(rec),
        **kw,
    )
    return eng, rec


def sse(name: str) -> bytes:
    return (FIX / f"{name}.sse").read_bytes()


# ---- OpenAI Chat Completions ----


async def test_openai_plain_stream_contract() -> None:
    eng, rec = engine({"stream": sse("plain")}, api_key="k")
    await check_plain_stream(eng, plain_request(), "red")
    request = rec.requests[0]
    assert request.method == "POST" and request.url.path == OPENAI_CHAT
    assert request.headers["authorization"] == "Bearer k"
    assert "x-api-key" not in request.headers and "anthropic-version" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "small"
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 32
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "enable_thinking" not in body and "thinking_budget" not in body and "tool_parser" not in body
    assert body["messages"][0] == {"role": "system", "content": "Reply in one short sentence."}


async def test_openai_usage_comes_from_the_final_usage_frame() -> None:
    eng, _ = engine({"stream": sse("plain")})
    res = await eng.generate(plain_request())
    assert (res.prompt_tokens, res.completion_tokens) == (28, 13)
    assert res.cached_tokens == 0  # usage.prompt_tokens_details.cached_tokens


async def test_openai_reasoning_deltas_are_separated_from_content() -> None:
    eng, rec = engine({"stream": sse("reasoning")})
    seen: list[TokenEvent] = []
    req = plain_request()
    req.thinking = True
    res = await eng.generate(req, on_token=seen.append)
    assert seen[0].reasoning is True
    assert res.reasoning.startswith("\nOkay")
    assert "Okay" not in res.text
    assert res.reasoning == "".join(e.text for e in seen if e.reasoning)  # once per delta, not twice
    assert json.loads(rec.requests[0].content)["chat_template_kwargs"] == {"enable_thinking": True}


async def test_openai_the_reasoning_alias_alone_is_read() -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": None, "reasoning": "hmm"}}]},
        {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]},
    ]
    eng, _ = engine({"stream": _openai_sse(frames)})
    res = await eng.generate(plain_request())
    assert res.reasoning == "hmm" and res.text == "ok"


def _openai_sse(frames: list[dict[str, Any]]) -> bytes:
    return ("".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n").encode()


async def test_openai_resumed_prompt_reports_cached_tokens() -> None:
    eng, _ = engine({"stream": sse("resume")})
    res = await eng.generate(plain_request())
    assert res.cached_tokens == 16
    assert res.prompt_tokens == 49 and res.completion_tokens == 3
    assert res.draft_tokens is None and res.accepted_draft_tokens is None
    assert res.ttft_ms > 0


async def test_openai_draft_counters_are_read_from_timings() -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 40, "prompt_tokens_details": None},
            "timings": {"draft_n": 40, "draft_n_accepted": 31},
        },
    ]
    eng, _ = engine({"stream": _openai_sse(frames)})
    res = await eng.generate(plain_request())
    assert res.draft_tokens == 40 and res.accepted_draft_tokens == 31
    assert res.cached_tokens is None and res.completion_tokens == 40


async def test_openai_null_fields_are_tolerated() -> None:
    call = {"index": None, "id": "c1", "function": {"name": "read_file", "arguments": "{}"}}
    frames = [
        {
            "choices": [
                {"index": None, "delta": {"content": None, "tool_calls": [call]}, "finish_reason": None}
            ],
            "usage": {"prompt_tokens": None, "completion_tokens": None},
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    eng, _ = engine({"stream": _openai_sse(frames)})
    res = await eng.generate(_tool_request())
    assert [c.name for c in res.tool_calls] == ["read_file"] and res.prompt_tokens == 0


def _tool_request():  # type: ignore[no-untyped-def]
    req = plain_request()
    req.tools = [
        ToolSchema("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})
    ]
    return req


async def test_openai_tool_calls_are_merged_by_index_and_finish_is_tool_calls() -> None:
    eng, rec = engine({"stream": sse("tool_call")})
    res = await eng.generate(_tool_request())
    assert res.finish_reason == "tool_calls"
    assert len(res.tool_calls) == 1  # fragments of one call, merged by index
    assert res.tool_calls[0].name == "read_file"
    assert json.loads(res.tool_calls[0].arguments) == {"path": "README.md"}
    body = json.loads(rec.requests[0].content)
    assert body["tools"][0]["function"]["name"] == "read_file"
    assert body["tool_choice"] == "auto"


async def test_openai_fastapi_detail_error_becomes_engine_error() -> None:
    eng, _ = engine({"stream": (404, (FIX / "error_bad_model.json").read_bytes())})
    with pytest.raises(EngineError, match=r"404: Failed to load model"):
        await eng.generate(plain_request())


async def test_openai_error_frame_mid_stream_raises_with_partial_text() -> None:
    body = (
        b'data: {"choices":[{"index":0,"delta":{"content":"part"},"finish_reason":null}]}\n\n'
        b'data: {"error":{"message":"model busy"}}\n\n'
    )
    eng, _ = engine({"stream": body})
    with pytest.raises(EngineError, match="model busy") as ei:
        await eng.generate(plain_request())
    assert ei.value.partial_text == "part"


async def test_openai_truncated_stream_raises() -> None:
    eng, _ = engine(
        {"stream": b'data: {"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n\n'}
    )
    with pytest.raises(EngineError, match=r"without \[DONE\]"):
        await eng.generate(plain_request())


async def test_openai_yes_no_reads_a_constrained_text_answer() -> None:
    answer = {"choices": [{"message": {"content": "YES"}, "finish_reason": "stop"}], "usage": {}}
    eng, rec = engine({"nonstream": json.dumps(answer).encode()})
    p_yes, p_no = await eng.yes_no("judge", [Message(role="user", content="Is water wet?")])
    assert (p_yes, p_no) == (1.0, 0.0)
    request = rec.requests[0]
    assert request.url.path == OPENAI_CHAT
    body = json.loads(request.content)
    assert body["max_tokens"] <= 4 and body["stream"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert eng.capabilities.logprobs is False


async def test_openai_health_reads_a_plain_health_and_the_model_list() -> None:
    models = {"data": [{"id": "small"}, {"id": "big", "loaded": False}]}
    eng, rec = engine({"/health": b"", "/v1/models": json.dumps(models).encode()})
    h = await eng.health()
    assert h.ok is True
    assert h.models == {"small": "ready", "big": "lazy"}
    assert [r.url.path for r in rec.requests] == ["/health", "/v1/models"]


async def test_health_when_server_down() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    eng = HTTPEngine(
        "http://engine.test/v1", lambda r: "small", lambda r: role_spec(), transport=httpx.MockTransport(boom)
    )
    h = await eng.health()
    assert h.ok is False and "refused" in h.detail


async def test_cancel_closes_stream() -> None:
    eng, _ = engine({"stream": sse("plain")})
    req = plain_request()
    await eng.cancel(req.request_id)  # cancelling before start marks it; generate must return 'cancelled'
    res = await eng.generate(req)
    assert res.finish_reason == "cancelled"


def test_body_carries_repetition_penalty_when_set() -> None:
    """Live finding 2026-10-13: the coder collapses into repetition (thousands of '0's, repeated run_tests
    blocks) until max_tokens; a mild repetition_penalty is the per-request guard."""
    from airlie.config import Sampling
    from airlie.engine.types import GenRequest

    role = RoleSpec(prompt=Path(__file__), model="base", thinking=False, max_tokens=64)
    eng = HTTPEngine("http://x/v1", passthru_name_for_role=lambda r: "base", role_spec_for=lambda r: role)
    req = GenRequest(
        role="architect", messages=[Message(role="user", content="hi")], sampling=Sampling(), max_tokens=10
    )
    assert "repetition_penalty" not in eng._body(req, stream=False)  # noqa: SLF001 - 1.0 means off
    req.sampling = Sampling(repetition_penalty=1.05)
    assert eng._body(req, stream=False)["repetition_penalty"] == 1.05  # noqa: SLF001


async def test_message_parts_are_sent_as_content_list_and_role_kwargs_merge(tmp_path: Path) -> None:
    from airlie.engine import ContentPart, image_part

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    spec = role_spec(model="m", chat_template_kwargs={"reasoning_strength": "low"})
    eng, rec = engine({"stream": sse("plain")}, spec=spec)
    req = plain_request()
    req.messages = [
        Message(
            role="user",
            content="critique this",
            parts=(ContentPart("text", text="critique this"), image_part(png)),
        )
    ]
    await eng.generate(req)
    body = json.loads(rec.requests[0].content)
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "critique this"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    assert body["chat_template_kwargs"] == {"enable_thinking": False, "reasoning_strength": "low"}


# ---- Anthropic Messages ----


def ev(kind: str, **data: Any) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"


def anthropic_sse(*events: str) -> bytes:
    return "".join(events).encode()


def text_stream(*pieces: str, input_tokens: int = 12, output_tokens: int = 5) -> bytes:
    events = [
        ev(
            "message_start",
            message={
                "id": "msg_1",
                "model": "small",
                "role": "assistant",
                "content": [],
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        ),
        ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        *[
            ev("content_block_delta", index=0, delta={"type": "text_delta", "text": piece})
            for piece in pieces
        ],
        ev("content_block_stop", index=0),
        ev(
            "message_delta",
            delta={"stop_reason": "end_turn"},
            usage={"output_tokens": output_tokens},
        ),
        ev("message_stop"),
    ]
    return anthropic_sse(*events)


def anthropic_engine(routes: dict[str, bytes | tuple[int, bytes]], **kw: Any) -> tuple[HTTPEngine, Recorder]:
    kw.setdefault("api_key", "sk-ant")
    kw.setdefault("base_url", "https://api.anthropic.test/v1")
    return engine(routes, dialect="anthropic", **kw)


async def test_anthropic_stream_posts_to_v1_messages_with_anthropic_headers() -> None:
    eng, rec = anthropic_engine({"stream": text_stream("The ", "answer")})
    seen: list[TokenEvent] = []
    res = await eng.generate(plain_request(), on_token=seen.append)
    request = rec.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.anthropic.test/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    assert res.text == "The answer" and "".join(t.text for t in seen) == "The answer"
    assert res.finish_reason == "stop"
    assert (res.prompt_tokens, res.completion_tokens) == (12, 5)
    assert eng.stats().requests == 1 and eng.stats().tokens_out == 5


async def test_anthropic_request_body_is_a_messages_body() -> None:
    eng, rec = anthropic_engine({"stream": text_stream("ok")})
    await eng.generate(plain_request())
    body = json.loads(rec.requests[0].content)
    assert body["model"] == "small"
    assert body["system"] == "Reply in one short sentence."
    assert body["messages"] == [{"role": "user", "content": "Name three primary colours."}]
    assert body["max_tokens"] == 32 and body["stream"] is True
    assert body["temperature"] == 0.0
    assert not {"stream_options", "chat_template_kwargs", "min_p", "presence_penalty"} & set(body)


async def test_anthropic_without_a_key_sends_the_version_header_only() -> None:
    eng, rec = anthropic_engine({"stream": text_stream("ok")}, api_key=None)
    await eng.generate(plain_request())
    headers = rec.requests[0].headers
    assert headers["anthropic-version"] == "2023-06-01"
    assert "x-api-key" not in headers and "authorization" not in headers


async def test_anthropic_url_is_the_same_with_or_without_the_v1_suffix() -> None:
    for base in ("https://api.anthropic.test", "https://api.anthropic.test/v1/"):
        eng, rec = anthropic_engine({"stream": text_stream("ok")}, base_url=base)
        await eng.generate(plain_request())
        assert str(rec.requests[0].url) == "https://api.anthropic.test/v1/messages"


async def test_anthropic_sends_temperature_or_top_p_never_both() -> None:
    from airlie.config import Sampling

    eng, rec = anthropic_engine({"stream": text_stream("ok")})
    req = plain_request()
    req.sampling = Sampling(temperature=0.3, top_p=0.9)
    await eng.generate(req)
    body = json.loads(rec.requests[0].content)
    assert body["temperature"] == 0.3 and "top_p" not in body


async def test_anthropic_thinking_maps_to_a_budget_and_drops_sampling() -> None:
    spec = role_spec(thinking=True, thinking_budget=2000, max_tokens=4000)
    eng, rec = anthropic_engine({"stream": text_stream("ok")}, spec=spec)
    req = plain_request()
    req.max_tokens = 4000
    req.thinking = None
    await eng.generate(req)
    body = json.loads(rec.requests[0].content)
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2000}
    assert body["max_tokens"] == 4000
    assert not {"temperature", "top_p", "top_k"} & set(body)  # a thinking turn takes the server's sampling


async def test_anthropic_thinking_budget_is_clamped_below_max_tokens_and_skipped_when_too_small() -> None:
    spec = role_spec(thinking=True, thinking_budget=9000, max_tokens=4000)
    eng, rec = anthropic_engine({"stream": text_stream("ok")}, spec=spec)
    req = plain_request()
    req.max_tokens = 3000
    req.thinking = True
    await eng.generate(req)
    assert json.loads(rec.requests[0].content)["thinking"] == {"type": "enabled", "budget_tokens": 2999}
    tiny = plain_request()
    tiny.max_tokens = 500
    tiny.thinking = True
    await eng.generate(tiny)
    body = json.loads(rec.requests[1].content)
    assert "thinking" not in body and body["temperature"] == 0.0  # not enough room: no thinking


async def test_anthropic_thinking_off_sends_no_thinking_block() -> None:
    eng, rec = anthropic_engine({"stream": text_stream("ok")})
    req = plain_request()
    req.thinking = False
    await eng.generate(req)
    assert "thinking" not in json.loads(rec.requests[0].content)


async def test_anthropic_thinking_stream_fills_reasoning() -> None:
    events = [
        ev("message_start", message={"id": "m", "model": "small", "usage": {"input_tokens": 4}}),
        ev("content_block_start", index=0, content_block={"type": "thinking", "thinking": ""}),
        ev("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": "hmm"}),
        ev("content_block_stop", index=0),
        ev("content_block_start", index=1, content_block={"type": "text", "text": ""}),
        ev("content_block_delta", index=1, delta={"type": "text_delta", "text": "done"}),
        ev("content_block_stop", index=1),
        ev("message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 3}),
        ev("message_stop"),
    ]
    eng, _ = anthropic_engine({"stream": anthropic_sse(*events)})
    seen: list[TokenEvent] = []
    res = await eng.generate(plain_request(), on_token=seen.append)
    assert res.reasoning == "hmm" and res.text == "done"
    assert [(t.text, t.reasoning) for t in seen] == [("hmm", True), ("done", False)]


async def test_anthropic_tool_use_stream_becomes_tool_calls() -> None:
    events = [
        ev("message_start", message={"id": "m", "model": "small", "usage": {"input_tokens": 9}}),
        ev(
            "content_block_start",
            index=0,
            content_block={"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
        ),
        ev("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": '{"path":'}),
        ev(
            "content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": '"README.md"}'},
        ),
        ev("content_block_stop", index=0),
        ev("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 7}),
        ev("message_stop"),
    ]
    eng, rec = anthropic_engine({"stream": anthropic_sse(*events)})
    res = await eng.generate(_tool_request())
    assert res.finish_reason == "tool_calls"
    assert [(c.id, c.name) for c in res.tool_calls] == [("toolu_1", "read_file")]
    assert json.loads(res.tool_calls[0].arguments) == {"path": "README.md"}
    body = json.loads(rec.requests[0].content)
    assert body["tools"][0]["name"] == "read_file"
    assert "input_schema" in body["tools"][0] and body["tool_choice"] == {"type": "auto"}


async def test_anthropic_max_tokens_stop_is_length() -> None:
    events = text_stream("cut").decode().replace("end_turn", "max_tokens").encode()
    eng, _ = anthropic_engine({"stream": events})
    assert (await eng.generate(plain_request())).finish_reason == "length"


async def test_anthropic_error_status_raises_with_the_servers_message() -> None:
    body = json.dumps(
        {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    ).encode()
    eng, _ = anthropic_engine({"stream": (401, body)})
    with pytest.raises(EngineError, match=r"401: invalid x-api-key"):
        await eng.generate(plain_request())
    assert eng.stats().errors == 1


async def test_anthropic_error_event_mid_stream_raises_with_partial_text() -> None:
    events = [
        ev("message_start", message={"id": "m", "model": "small", "usage": {"input_tokens": 1}}),
        ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        ev("content_block_delta", index=0, delta={"type": "text_delta", "text": "part"}),
        ev("error", error={"type": "overloaded_error", "message": "Overloaded"}),
    ]
    eng, _ = anthropic_engine({"stream": anthropic_sse(*events)})
    with pytest.raises(EngineError, match="Overloaded") as ei:
        await eng.generate(plain_request())
    assert ei.value.partial_text == "part"


async def test_anthropic_error_event_before_any_content_raises_not_empty_success() -> None:
    events = [
        ev("message_start", message={"id": "m", "model": "small", "usage": {"input_tokens": 1}}),
        ev("error", error={"type": "overloaded_error", "message": "Overloaded"}),
    ]
    eng, _ = anthropic_engine({"stream": anthropic_sse(*events)})
    with pytest.raises(EngineError, match="Overloaded") as ei:
        await eng.generate(plain_request())
    assert ei.value.partial_text == ""
    assert eng.stats().errors == 1


async def test_anthropic_stream_without_message_stop_raises() -> None:
    cut = text_stream("pa", "rt").decode().split("event: message_delta")[0].encode()
    eng, _ = anthropic_engine({"stream": cut})
    with pytest.raises(EngineError, match="ended without") as ei:
        await eng.generate(plain_request())
    assert ei.value.partial_text == "part"


class _Chunked(httpx.AsyncByteStream):
    """The same SSE bytes, cut into 7-byte pieces: a frame never arrives whole."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for i in range(0, len(self.data), 7):
            yield self.data[i : i + 7]


async def test_anthropic_stream_survives_arbitrary_chunk_boundaries() -> None:
    data = text_stream("one ", "two ", "three")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunked(data), headers={"content-type": "text/event-stream"})

    eng = HTTPEngine(
        "https://api.anthropic.test/v1",
        lambda r: "small",
        lambda r: role_spec(),
        transport=httpx.MockTransport(handler),
        dialect="anthropic",
    )
    res = await eng.generate(plain_request())
    assert res.text == "one two three" and res.finish_reason == "stop"


async def test_anthropic_yes_no_posts_a_non_streamed_message() -> None:
    answer = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "small",
        "content": [{"type": "text", "text": "NO"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    eng, rec = anthropic_engine({"nonstream": json.dumps(answer).encode()})
    assert await eng.yes_no("judge", [Message(role="user", content="Is water dry?")]) == (0.0, 1.0)
    request = rec.requests[0]
    assert str(request.url) == "https://api.anthropic.test/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant"
    body = json.loads(request.content)
    assert "stream" not in body and body["max_tokens"] == 3
    assert body["system"].startswith("Answer with exactly one word")
    assert body["messages"] == [{"role": "user", "content": "Is water dry?"}]
    assert "thinking" not in body


async def test_anthropic_yes_no_error_status_raises() -> None:
    body = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})
    eng, _ = anthropic_engine({"nonstream": (429, body.encode())})
    with pytest.raises(EngineError, match=r"429: slow down"):
        await eng.yes_no("judge", [Message(role="user", content="q")])


async def test_anthropic_health_lists_models_with_the_key() -> None:
    models = {"data": [{"id": "claude-a", "type": "model"}, {"id": "claude-b", "type": "model"}]}
    eng, rec = anthropic_engine({"/v1/models": json.dumps(models).encode()})
    h = await eng.health()
    assert h.ok is True and h.models == {"claude-a": "ready", "claude-b": "ready"}
    assert [r.url.path for r in rec.requests] == ["/v1/models"]  # Anthropic has no /health
    assert rec.requests[0].headers["x-api-key"] == "sk-ant"
    assert rec.requests[0].headers["anthropic-version"] == "2023-06-01"


async def test_anthropic_health_reports_a_denied_key() -> None:
    eng, _ = anthropic_engine({"/v1/models": (401, b'{"type":"error","error":{"message":"bad key"}}')})
    h = await eng.health()
    assert h.ok is False and "401" in h.detail
