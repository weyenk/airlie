"""HTTPEngine wire format, health, tool-call merging, timing, cancellation and yes/no, pinned exactly."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import airlie.engine.http_engine as he
from airlie.config import RoleSpec, Sampling
from airlie.engine import ContentPart, EngineError, GenRequest, Message, ToolCall, ToolSchema
from airlie.engine.http_engine import YES_NO_SYSTEM, HTTPEngine, _error_text, _finish, _wire_message
from airlie.engine.types import EngineCapabilities, EngineHealth, EngineStats, TokenEvent
from tests.unit.vllm_helpers import ChatCompletionRequest


def role_spec(**kw: Any) -> RoleSpec:
    base: dict[str, Any] = {
        "prompt": Path("/dev/null"),
        "model": "m",
        "thinking": False,
        "thinking_budget": None,
        "max_tokens": 64,
    }
    return RoleSpec(**{**base, **kw})


def stub(routes: dict[str, Any]):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        r = routes[request.url.path]
        if callable(r):
            return r(request)  # type: ignore[no-any-return]
        status, content = r if isinstance(r, tuple) else (200, r)
        ctype = "text/event-stream" if content.startswith(b"data:") else "application/json"
        return httpx.Response(status, content=content, headers={"content-type": ctype})

    return seen, httpx.MockTransport(handler)


def make(routes: dict[str, Any] | None = None, spec: RoleSpec | None = None, **kw: Any):  # type: ignore[no-untyped-def]
    seen, transport = stub(routes or {})
    e = HTTPEngine(
        "http://engine.test/v1",
        lambda role: f"served-{role}",
        lambda role: spec or role_spec(),
        transport=transport,
        **kw,
    )
    return e, seen


def req(**kw: Any) -> GenRequest:
    base: dict[str, Any] = {
        "role": "coder",
        "messages": [Message(role="user", content="hi")],
        "sampling": Sampling(),
        "max_tokens": 7,
    }
    return GenRequest(**{**base, **kw})


def frame(delta: dict[str, Any] | None = None, finish: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}


def sse(*frames: dict[str, Any], done: bool = True) -> bytes:
    out = "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
    return (out + ("data: [DONE]\n\n" if done else "")).encode()


CHAT = "/v1/chat/completions"


# ---- construction and lifecycle ----


def test_constructor_normalises_urls_headers_and_capabilities() -> None:
    e = HTTPEngine("http://h:1/v1/", lambda r: "x", lambda r: role_spec(), api_key="k")
    assert (e.base_url, e._root) == ("http://h:1/v1", "http://h:1")
    assert e._headers == {"Authorization": "Bearer k"}
    assert e.capabilities == EngineCapabilities(True, True, False, True, per_request_draft=False)
    assert e.stats() == EngineStats(0, 0, 0)
    bare = HTTPEngine("http://h:1", lambda r: "x", lambda r: role_spec(), per_request_draft=True)
    assert (bare.base_url, bare._root, bare._headers) == ("http://h:1", "http://h:1", {})
    assert bare.capabilities.per_request_draft is True
    assert HTTPEngine("http://h/v1", lambda r: "x", lambda r: role_spec(), api_key="")._headers == {}


async def test_client_is_created_once_with_timeouts_and_headers_and_stop_resets_it() -> None:
    e = HTTPEngine("http://h/v1", lambda r: "x", lambda r: role_spec(), api_key="k")
    c = e._c()
    assert c is e._c()
    assert c.timeout == httpx.Timeout(600.0, connect=10.0)
    assert c.headers["authorization"] == "Bearer k"
    await e.stop()
    assert e._client is None and c.is_closed
    await e.stop()  # stopping twice is fine
    await e.start()
    assert e._client is not None and e._client is not c
    short = HTTPEngine("http://h/v1", lambda r: "x", lambda r: role_spec(), timeout_s=5.0)
    assert short._c().timeout == httpx.Timeout(5.0, connect=10.0)
    await short.stop()
    await e.stop()


# ---- health ----

HEALTH = b'{"status":"healthy","engine_type":"batched"}'
MODELS = b'{"data":[{"id":"small"},{"id":"big"}]}'


async def test_health_reports_models_and_detail_from_the_two_endpoints() -> None:
    e, seen = make({"/health": HEALTH, "/v1/models": MODELS})
    h = await e.health()
    assert h == EngineHealth(True, {"small": "ready", "big": "ready"}, "healthy engine=batched")
    assert [r.url.path for r in seen] == ["/health", "/v1/models"]


async def test_health_with_unhealthy_server_or_missing_models() -> None:
    e, _ = make({"/health": (503, b"{}"), "/v1/models": MODELS})
    assert await e.health() == EngineHealth(False, {"small": "ready", "big": "ready"}, "health 503")
    e, _ = make({"/health": HEALTH, "/v1/models": (500, b"{}")})
    assert await e.health() == EngineHealth(False, {}, "healthy engine=batched")
    e, _ = make({"/health": b"{}", "/v1/models": b"{}"})
    assert await e.health() == EngineHealth(True, {}, "? engine=?")


async def test_health_transport_error_is_reported_not_raised() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    e, _ = make({"/health": boom, "/v1/models": boom})
    assert await e.health() == EngineHealth(False, {}, "ConnectError: refused")


# ---- request body ----


def test_body_has_every_sampling_field_and_the_served_name() -> None:
    e, _ = make()
    r = req(sampling=Sampling(temperature=0.3, top_p=0.9, top_k=5, min_p=0.1, presence_penalty=0.2))
    assert e._body(r, stream=False) == {
        "model": "served-coder",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "max_tokens": 7,
        "temperature": 0.3,
        "top_p": 0.9,
        "top_k": 5,
        "min_p": 0.1,
        "presence_penalty": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
        "enable_thinking": False,
    }
    streamed = e._body(r, stream=True)
    assert streamed["stream"] is True and streamed["stream_options"] == {"include_usage": True}
    assert "stream_options" not in e._body(r, stream=False)


def test_body_optional_fields_appear_only_when_set() -> None:
    e, _ = make()
    plain = e._body(req(), stream=False)
    keys = ("stop", "tools", "tool_choice", "thinking_token_budget", "num_draft_tokens", "repetition_penalty")
    for key in keys:
        assert key not in plain
    stopped = e._body(req(stop=["END"]), stream=False)
    assert stopped["stop"] == ["END"]
    tools = [ToolSchema("read_file", "Read", {"type": "object"}), ToolSchema("grep", "Search", {"a": 1})]
    withtools = e._body(req(tools=tools), stream=False)
    assert withtools["tool_choice"] == "auto"
    assert withtools["tools"] == [
        {
            "type": "function",
            "function": {"name": "read_file", "description": "Read", "parameters": {"type": "object"}},
        },
        {"type": "function", "function": {"name": "grep", "description": "Search", "parameters": {"a": 1}}},
    ]
    assert "tools" not in e._body(req(tools=[]), stream=False)
    assert e._body(req(sampling=Sampling(repetition_penalty=1.1)), stream=False)["repetition_penalty"] == 1.1


def test_thinking_comes_from_the_request_over_the_role_and_gates_the_thinking_token_budget() -> None:
    on = role_spec(thinking=True, thinking_budget=100)
    e, _ = make(spec=on)
    assert e._body(req(), stream=False)["chat_template_kwargs"] == {"enable_thinking": True}
    body = e._body(req(), stream=False)
    assert body["thinking_token_budget"] == 100
    assert [k for k in body if "budget" in k] == ["thinking_token_budget"]  # under no other name
    off = e._body(req(thinking=False), stream=False)
    assert off["chat_template_kwargs"] == {"enable_thinking": False} and "thinking_token_budget" not in off
    e2, _ = make(spec=role_spec(thinking=False, thinking_budget=100))
    forced = e2._body(req(thinking=True), stream=False)
    assert forced["chat_template_kwargs"] == {"enable_thinking": True}
    assert forced["thinking_token_budget"] == 100
    assert "thinking_token_budget" not in e2._body(req(), stream=False)
    e3, _ = make(spec=role_spec(thinking=True, thinking_budget=None))
    assert "thinking_token_budget" not in e3._body(req(), stream=False)


def test_vllm_mlx_reads_the_budget_from_the_body_the_engine_sends() -> None:
    """vllm-mlx 0.5.0 drops request keys its pydantic model does not know, and reads the per-request budget
    from `thinking_token_budget` (it never reads `thinking_budget`): its own request model, given our body,
    must come out with the role's budget."""
    e, _ = make(spec=role_spec(thinking=True, thinking_budget=100, max_tokens=4096))
    tools = [ToolSchema("read_file", "Read", {"type": "object"})]
    body = e._body(req(tools=tools, stop=["END"], max_tokens=4096), stream=True)
    read = ChatCompletionRequest.model_validate(body)
    assert read.thinking_token_budget == 100
    assert read.max_tokens == 4096 and read.stop == ["END"]
    assert read.chat_template_kwargs == {"enable_thinking": True}


def test_role_template_kwargs_merge_over_the_thinking_flag() -> None:
    e, _ = make(spec=role_spec(chat_template_kwargs={"reasoning_strength": "low", "enable_thinking": True}))
    assert e._body(req(), stream=False)["chat_template_kwargs"] == {
        "enable_thinking": True,
        "reasoning_strength": "low",
    }


def test_draft_budget_is_sent_only_when_the_engine_supports_it() -> None:
    plain, _ = make()
    assert "num_draft_tokens" not in plain._body(req(draft_budget=3), stream=False)
    cap, _ = make(per_request_draft=True)
    assert cap._body(req(draft_budget=3), stream=False)["num_draft_tokens"] == 3
    assert cap._body(req(draft_budget=0), stream=False)["num_draft_tokens"] == 0
    assert "num_draft_tokens" not in cap._body(req(), stream=False)


# ---- generation ----


async def test_generate_posts_the_streaming_body_and_counts_the_request() -> None:
    e, seen = make({CHAT: sse(frame({"content": "ok"}, "stop"))})
    res = await e.generate(req())
    assert seen[0].method == "POST" and seen[0].url.path == CHAT
    assert json.loads(seen[0].content)["stream"] is True
    assert (res.text, res.finish_reason, res.raw_finish) == ("ok", "stop", "stop")
    assert e.stats().requests == 1


async def test_tokens_stream_to_the_callback_in_order_with_the_reasoning_flag() -> None:
    e, _ = make(
        {CHAT: sse(frame({"reasoning_content": "think"}), frame({"content": "say"}), frame({}, "stop"))}
    )
    events: list[TokenEvent] = []
    res = await e.generate(req(), on_token=events.append)
    assert events == [TokenEvent("think", reasoning=True), TokenEvent("say")]
    assert (res.reasoning, res.text) == ("think", "say")
    res2 = await e.generate(req())  # no callback is fine
    assert res2.text == "say"


async def test_live_response_is_registered_during_the_stream_and_cleared_after() -> None:
    e, _ = make({CHAT: sse(frame({"content": "a"}), frame({"content": "b"}, "stop"))})
    r = req()
    during: list[bool] = []
    await e.generate(
        r, on_token=lambda ev: during.append(isinstance(e._live.get(r.request_id), httpx.Response))
    )
    assert during == [True, True]
    assert e._live == {} and e._cancelled == set()


async def test_speculative_decoding_metadata_is_read_including_zero() -> None:
    e, _ = make(
        {
            CHAT: sse(
                frame(generation_metadata={"mtp_accepted": 0, "mtp_drafts": 4}),
                frame(generation_metadata="ignored", finish="stop"),
            )
        }
    )
    res = await e.generate(req())
    assert (res.accepted_draft_tokens, res.draft_tokens) == (0, 4)
    only, _ = make({CHAT: sse(frame(generation_metadata={"mtp_drafts": 2}, finish="stop"))})
    res = await only.generate(req())
    assert (res.accepted_draft_tokens, res.draft_tokens) == (None, 2)
    none, _ = make({CHAT: sse(frame(finish="stop"))})
    res = await none.generate(req())
    assert (res.accepted_draft_tokens, res.draft_tokens) == (None, None)


async def test_usage_defaults_keep_earlier_values_and_cached_tokens_may_be_zero() -> None:
    e, _ = make(
        {
            CHAT: sse(
                frame(usage={"prompt_tokens": 11, "completion_tokens": 5}),
                frame(usage={"prompt_tokens_details": {"cached_tokens": 0}}, finish="stop"),
            )
        }
    )
    res = await e.generate(req())
    assert (res.prompt_tokens, res.completion_tokens, res.cached_tokens) == (11, 5, 0)
    assert e.stats().tokens_out == 5
    plain, _ = make({CHAT: sse(frame(usage={"prompt_tokens_details": {"other": 1}}, finish="stop"))})
    res = await plain.generate(req())
    assert (res.prompt_tokens, res.completion_tokens, res.cached_tokens) == (0, 0, None)
    odd, _ = make({CHAT: sse(frame(usage="n/a", finish="stop"))})
    assert (await odd.generate(req())).prompt_tokens == 0
    flat, _ = make({CHAT: sse(frame(usage={"prompt_tokens_details": 3, "prompt_tokens": 2}, finish="stop"))})
    assert (await flat.generate(req())).cached_tokens is None


async def test_tokens_out_accumulates_across_requests() -> None:
    e, _ = make({CHAT: sse(frame(usage={"completion_tokens": 4}, finish="stop"))})
    await e.generate(req())
    await e.generate(req())
    assert e.stats() == EngineStats(requests=2, tokens_out=8, errors=0)


def tc(index: int | None = None, id: str | None = None, name: str | None = None, args: str | None = None):  # type: ignore[no-untyped-def]
    d: dict[str, Any] = {}
    if index is not None:
        d["index"] = index
    if id is not None:
        d["id"] = id
    fn: dict[str, Any] = {}
    if name is not None:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    if fn:
        d["function"] = fn
    return d


async def test_tool_call_fragments_are_assembled_per_index_in_order() -> None:
    frames = [
        frame({"tool_calls": [tc(1, "call_b", "grep", '{"p":')]}),
        frame({"tool_calls": [tc(1, None, None, '"x"}')]}),
        frame({"tool_calls": [tc(0, "call_a", "read_file", '{"path": "a"}')]}),
        frame({"tool_calls": [tc(0, "call_other", None, '{"path": "a"}')]}, "stop"),
    ]
    e, _ = make({CHAT: sse(*frames)})
    res = await e.generate(req())
    assert res.tool_calls == [
        ToolCall("call_a", "read_file", '{"path": "a"}'),
        ToolCall("call_b", "grep", '{"p":"x"}'),
    ]
    assert (res.finish_reason, res.raw_finish) == ("tool_calls", "tool_calls")


async def test_an_argument_fragment_that_starts_with_a_brace_is_appended() -> None:
    """A streamed value chunk may start with `{` (an f-string, a dict literal); it continues the call
    (2026-10-14: `{self.id}` replaced everything before it and the call lost its path)."""
    frames = [
        frame({"tool_calls": [tc(0, "a", "write_file", '{"path": "x.py", "content": "f\\"')]}),
        frame({"tool_calls": [tc(0, None, None, "{self.id}")]}),
        frame({"tool_calls": [tc(0, None, None, ' {}\\""}')]}, "tool_calls"),
    ]
    e, _ = make({CHAT: sse(*frames)})
    res = await e.generate(req())
    assert res.tool_calls == [
        ToolCall("a", "write_file", '{"path": "x.py", "content": "f\\"{self.id} {}\\""}')
    ]


async def test_fragments_repeating_the_same_id_are_appended() -> None:
    frames = [
        frame({"tool_calls": [tc(0, "a", "f", '{"o": ')]}),
        frame({"tool_calls": [tc(0, "a", None, '{"b": 2}}')]}, "tool_calls"),
    ]
    e, _ = make({CHAT: sse(*frames)})
    assert (await e.generate(req())).tool_calls == [ToolCall("a", "f", '{"o": {"b": 2}}')]


async def test_a_call_resent_whole_under_a_new_id_is_kept_once() -> None:
    """vllm-mlx 0.5.0 repeats a finished call (same index, new id, whole arguments): keep it once."""
    frames = [
        frame({"tool_calls": [tc(0, "a", "read_file", '{"path": "README.md"}')]}),
        frame({"tool_calls": [tc(0, "b", "read_file", '{"path": "README.md"}')]}, "tool_calls"),
    ]
    e, _ = make({CHAT: sse(*frames)})
    assert (await e.generate(req())).tool_calls == [ToolCall("a", "read_file", '{"path": "README.md"}')]


async def test_an_id_arriving_after_the_first_fragment_keeps_the_arguments() -> None:
    frames = [
        frame({"tool_calls": [tc(0, None, "f", '{"a": ')]}),
        frame({"tool_calls": [tc(0, "x", None, "1}")]}, "tool_calls"),
    ]
    e, _ = make({CHAT: sse(*frames)})
    assert (await e.generate(req())).tool_calls == [ToolCall("x", "f", '{"a": 1}')]


async def test_tool_call_defaults_index_zero_and_stringifies_ids() -> None:
    e, _ = make({CHAT: sse(frame({"tool_calls": [{"id": 7, "function": {"name": "f"}}]}, "stop"))})
    res = await e.generate(req())
    assert res.tool_calls == [ToolCall("7", "f", "")]
    later, _ = make(
        {CHAT: sse(frame({"tool_calls": [tc(0, "a", "one")]}), frame({"tool_calls": [tc(0, "b", "two")]}))}
    )
    res = await later.generate(req())
    assert res.tool_calls == [ToolCall("a", "two", "")]  # first id kept, latest name wins


async def test_finish_reasons_are_normalised_and_raw_is_kept() -> None:
    for sent, normal in (
        ("length", "length"),
        ("stop", "stop"),
        ("weird", "stop"),
        ("tool_calls", "tool_calls"),
    ):
        e, _ = make({CHAT: sse(frame({"content": "x"}, sent))})
        res = await e.generate(req())
        assert (res.finish_reason, res.raw_finish) == (normal, sent)
    e, _ = make({CHAT: sse(frame({"content": "x"}))})
    res = await e.generate(req())
    assert (res.finish_reason, res.raw_finish) == ("stop", "stop")


def test_finish_maps_known_values_and_defaults_to_stop() -> None:
    for known in ("stop", "length", "tool_calls", "cancelled"):
        assert _finish(known) == known
    assert _finish("content_filter") == "stop" and _finish("") == "stop"


async def test_cancel_before_start_returns_a_cancelled_result_without_a_request() -> None:
    e, seen = make({CHAT: sse(frame({"content": "x"}, "stop"))})
    r = req()
    await e.cancel(r.request_id)
    res = await e.generate(r)
    assert (res.text, res.reasoning, res.tool_calls, res.finish_reason) == ("", "", [], "cancelled")
    assert (res.prompt_tokens, res.completion_tokens, res.raw_finish) == (0, 0, "cancelled")
    assert seen == [] and e.stats().requests == 0 and e._cancelled == set()
    assert (await e.generate(r)).text == "x"  # the id is free again


async def test_cancel_mid_stream_stops_reading_and_keeps_the_cancelled_finish() -> None:
    frames = [
        frame({"content": "a", "tool_calls": [tc(0, "c", "f", "{}")]}),
        frame({"content": "b"}),
        frame({"content": "c"}, "stop"),
    ]
    e, _ = make({CHAT: sse(*frames)})
    r = req()
    res = await e.generate(r, on_token=lambda ev: e._cancelled.add(r.request_id))
    assert res.text == "a" and res.finish_reason == "cancelled" and res.raw_finish == "cancelled"
    assert len(res.tool_calls) == 1  # the call that arrived with the first token is kept
    assert e._cancelled == set() and e._live == {}


async def test_cancel_closes_the_live_response() -> None:
    closed: list[bool] = []

    class Live:
        async def aclose(self) -> None:
            closed.append(True)

    e, _ = make()
    e._live["r1"] = Live()  # type: ignore[assignment]
    await e.cancel("r1")
    await e.cancel("unknown")
    assert closed == [True] and e._cancelled == {"r1", "unknown"}


async def test_http_error_status_becomes_an_engine_error_and_is_counted() -> None:
    e, _ = make({CHAT: (500, b'{"error": {"message": "boom"}}')})
    with pytest.raises(EngineError) as ei:
        await e.generate(req())
    assert str(ei.value) == "engine returned 500: boom" and ei.value.partial_text == ""
    assert e.stats().errors == 1 and e.stats().requests == 1
    assert e._live == {} and e._cancelled == set()


async def test_transport_failure_becomes_an_engine_error_and_is_counted() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    e, _ = make({CHAT: boom})
    with pytest.raises(EngineError) as ei:
        await e.generate(req())
    assert str(ei.value) == "ConnectError: refused" and ei.value.partial_text == ""
    assert e.stats().errors == 1


async def test_truncated_stream_keeps_the_partial_text_and_counts_the_error() -> None:
    e, _ = make({CHAT: sse(frame({"content": "par"}), frame({"content": "tial"}), done=False)})
    with pytest.raises(EngineError) as ei:
        await e.generate(req())
    assert ei.value.partial_text == "partial" and e.stats().errors == 1


def fake_clock(*values: float):  # type: ignore[no-untyped-def]
    it = iter(values)
    last = [values[-1]]

    def monotonic() -> float:
        v = next(it, None)
        if v is None:
            return last[0]
        last[0] = v
        return v

    return SimpleNamespace(monotonic=monotonic)


async def test_timing_ttft_and_decode_rate_come_from_the_token_timestamps(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # t0=0; first token at 10; second at 12; the closing frame at 13 does not move t_last
    monkeypatch.setattr(he, "time", fake_clock(0.0, 10.0, 12.0, 13.0))
    frames = [
        frame({"content": "a"}),
        frame({"reasoning_content": "b"}),
        frame({}, "stop", usage={"completion_tokens": 5}),
    ]
    e, _ = make({CHAT: sse(*frames)})
    res = await e.generate(req())
    assert res.ttft_ms == 10000.0
    assert res.decode_tps == 5 / 2.0


async def test_timing_single_token_has_no_decode_rate_and_tool_calls_count_as_first_token(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(he, "time", fake_clock(1.0, 3.0, 4.0))
    e, _ = make({CHAT: sse(frame({"content": "a"}, "stop", usage={"completion_tokens": 1}))})
    res = await e.generate(req())
    assert res.ttft_ms == 2000.0 and res.decode_tps == 0.0
    monkeypatch.setattr(he, "time", fake_clock(0.0, 2.0, 5.0))
    e2, _ = make(
        {
            CHAT: sse(
                frame({"tool_calls": [tc(0, "c", "f", "{}")]}),
                frame({}, "stop", usage={"completion_tokens": 3}),
            )
        }
    )
    res = await e2.generate(req())
    assert res.ttft_ms == 2000.0
    assert res.decode_tps == 3 / 1e-6  # first and last token were the same instant


async def test_timing_takes_the_engines_first_output_time_over_a_tool_call_held_back_to_the_end(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """vllm-mlx sends a parsed call whole at the end: its arrival is no first token (AIR-46 refine read 1,015
    tok/s). The engine's own time (patches/first_output.py) splits the request."""
    monkeypatch.setattr(he, "time", fake_clock(0.0, 5.0, 5.0))
    frames = [
        frame({"tool_calls": [tc(0, "c", "f", "{}")]}),
        frame({}, "tool_calls", usage={"completion_tokens": 21}, airlie_ttft_ms=1500.0),
    ]
    e, _ = make({CHAT: sse(*frames)})
    res = await e.generate(req())
    assert res.ttft_ms == 1500.0
    assert res.decode_tps == 21 / 3.5


async def test_timing_without_the_engines_time_counts_a_tool_call_only_when_nothing_streamed_before(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(he, "time", fake_clock(0.0, 1.0, 4.0))
    frames = [
        frame({"content": "Reading it."}),
        frame({"tool_calls": [tc(0, "c", "f", "{}")]}),
        frame({}, "tool_calls", usage={"completion_tokens": 6}),
    ]
    e, _ = make({CHAT: sse(*frames)})
    res = await e.generate(req())
    assert res.ttft_ms == 1000.0 and res.decode_tps == 6 / 3.0


async def test_timing_without_any_token_is_zero_ttft(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(he, "time", fake_clock(5.0, 6.0))
    e, _ = make({CHAT: sse(frame({}, "stop", usage={"completion_tokens": 3}))})
    res = await e.generate(req())
    assert res.ttft_ms == 0.0 and res.decode_tps == 3 / 1e-6


async def test_model_spec_for_gets_the_role_when_parsing_output() -> None:
    from airlie.config import ModelSpec

    roles: list[str] = []

    def model_spec_for(role: str) -> ModelSpec:
        roles.append(role)
        return ModelSpec(repo="o/m", revision="r")

    e, _ = make({CHAT: sse(frame({"content": "plain"}, "stop"))}, model_spec_for=model_spec_for)
    res = await e.generate(req(role="judge"))
    assert roles == ["judge"] and res.text == "plain"


# ---- yes / no ----


def verdict(content: Any) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


async def test_yes_no_sends_the_constrained_request() -> None:
    e, seen = make({CHAT: verdict("YES")})
    msgs = [Message("system", "ignore me"), Message("user", "Is it?"), Message("assistant", "maybe")]
    assert await e.yes_no("judge", msgs) == (1.0, 0.0)
    assert json.loads(seen[0].content) == {
        "model": "served-judge",
        "messages": [
            {"role": "system", "content": YES_NO_SYSTEM},
            {"role": "user", "content": "Is it?"},
            {"role": "assistant", "content": "maybe"},
        ],
        "stream": False,
        "max_tokens": 3,
        "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False},
        "enable_thinking": False,
    }
    assert e.stats().requests == 1 and e.stats().errors == 0


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("YES", (1.0, 0.0)),
        ("yes", (1.0, 0.0)),
        ("  **Yes**, because", (1.0, 0.0)),
        ('"YES."', (1.0, 0.0)),
        ("'no'", (0.0, 1.0)),
        ("NO", (0.0, 1.0)),
        ("No.", (0.0, 1.0)),
        ("*No*", (0.0, 1.0)),
    ],
)
async def test_yes_no_parses_the_first_word(content: str, expected: tuple[float, float]) -> None:
    e, _ = make({CHAT: verdict(content)})
    assert await e.yes_no("judge", [Message("user", "q")]) == expected


@pytest.mark.parametrize("content", ["maybe", "", None, "   ", "I think YES"])
async def test_yes_no_rejects_anything_else_with_the_raw_answer(content: Any) -> None:
    e, _ = make({CHAT: verdict(content)})
    shown = content if content is not None else ""
    with pytest.raises(EngineError) as ei:
        await e.yes_no("judge", [Message("user", "q")])
    assert str(ei.value) == f"yes_no: role 'judge' answered {shown!r}, not YES/NO"


async def test_yes_no_http_error_is_counted() -> None:
    e, _ = make({CHAT: (503, b'{"detail": "down"}')})
    with pytest.raises(EngineError) as ei:
        await e.yes_no("judge", [Message("user", "q")])
    assert str(ei.value) == "engine returned 503: down"
    assert e.stats().errors == 1 and e.stats().requests == 1


# ---- wire messages and error text ----


def test_wire_message_minimal_and_with_every_optional_field() -> None:
    assert _wire_message(Message("user", "hi")) == {"role": "user", "content": "hi"}
    full = Message(
        "assistant",
        "text",
        name="n",
        tool_call_id="tc1",
        tool_calls=(ToolCall("c1", "f", "{}"), ToolCall("c2", "g", '{"a":1}')),
    )
    assert _wire_message(full) == {
        "role": "assistant",
        "content": "text",
        "name": "n",
        "tool_call_id": "tc1",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "g", "arguments": '{"a":1}'}},
        ],
    }
    assert "tool_calls" not in _wire_message(Message("assistant", "x", tool_calls=()))


def test_wire_message_parts_replace_the_content() -> None:
    m = Message(
        "user",
        "caption",
        parts=(ContentPart("text", text="caption"), ContentPart("image_url", url="data:image/png;base64,AA")),
    )
    assert _wire_message(m)["content"] == [
        {"type": "text", "text": "caption"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
    ]
    assert _wire_message(Message("user", "t", parts=()))["content"] == "t"


def test_error_text_understands_the_common_error_shapes() -> None:
    assert _error_text(b'{"error": {"message": "busy"}}') == "busy"
    assert _error_text(b'{"error": {"code": 7}}') == "{'code': 7}"
    assert _error_text(b'{"detail": "model x does not exist"}') == "model x does not exist"
    assert _error_text(b'{"error": "flat", "other": 1}') == "{'error': 'flat', 'other': 1}"
    assert _error_text(b'["a", "b"]') == "['a', 'b']"
    assert _error_text(b"plain text failure") == "plain text failure"
    assert _error_text(b"x" * 400) == "x" * 300
    assert _error_text(b"\xff\xfebad") == "��bad"
    long_obj = json.dumps({"k": "v" * 400}).encode()
    assert _error_text(long_obj) == str({"k": "v" * 400})[:300]
    assert _error_text(b'{"detail": null}') == "None"


# ---- follow-ups from the mutation run ----


def test_only_slashes_are_stripped_from_the_base_url() -> None:
    e = HTTPEngine("http://h/xX//", lambda r: "x", lambda r: role_spec())
    assert e.base_url == "http://h/xX"


def test_the_role_spec_is_looked_up_for_the_requests_own_role() -> None:
    specs = {"deep": role_spec(thinking=True), "quick": role_spec(thinking=False)}
    seen, transport = stub({})
    e = HTTPEngine("http://h/v1", lambda r: r, lambda r: specs[r], transport=transport)
    assert e._body(req(role="deep"), stream=False)["chat_template_kwargs"] == {"enable_thinking": True}
    assert e._body(req(role="quick"), stream=False)["chat_template_kwargs"] == {"enable_thinking": False}


async def test_a_tool_call_without_index_is_call_zero_and_a_nameless_call_has_an_empty_name() -> None:
    frames = [
        frame({"tool_calls": [tc(1, "b", "g", "{}")]}),
        frame({"tool_calls": [{"id": "a", "function": {"arguments": "{}"}}]}, "stop"),
    ]
    e, _ = make({CHAT: sse(*frames)})
    res = await e.generate(req())
    assert res.tool_calls == [ToolCall("a", "", "{}"), ToolCall("b", "g", "{}")]


async def test_error_counter_accumulates_for_every_kind_of_failure() -> None:
    e, _ = make({CHAT: sse(frame({"content": "x"}), done=False)})
    for _ in range(2):
        with pytest.raises(EngineError):
            await e.generate(req())
    assert e.stats().errors == 2

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    e2, _ = make({CHAT: boom})
    for _ in range(2):
        with pytest.raises(EngineError):
            await e2.generate(req())
    assert e2.stats().errors == 2


class _CutStream(httpx.AsyncByteStream):
    """200 OK, two content frames, then the connection drops."""

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield sse(frame({"content": "pa"}), frame({"content": "rt"}), done=False)
        raise httpx.ReadError("cut")


async def test_a_connection_drop_mid_stream_keeps_the_partial_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_CutStream(), headers={"content-type": "text/event-stream"})

    e, _ = make({CHAT: handler})
    with pytest.raises(EngineError) as ei:
        await e.generate(req())
    assert str(ei.value) == "ReadError: cut" and ei.value.partial_text == "part"
    assert e.stats().errors == 1


async def test_timing_each_kind_of_first_token_starts_the_decode_window(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # t0=0; first token at 2; a later token at 4: the decode window is 2s whichever branch came first
    def run(first: dict[str, Any], later: dict[str, Any], tokens: int):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(he, "time", fake_clock(0.0, 2.0, 4.0, 5.0))
        e, _ = make(
            {CHAT: sse(frame(first), frame(later), frame({}, "stop", usage={"completion_tokens": tokens}))}
        )
        return e.generate(req())

    for first, later in (
        ({"tool_calls": [tc(0, "c", "f", "{}")]}, {"content": "x"}),
        ({"reasoning_content": "r"}, {"content": "x"}),
        ({"content": "x"}, {"reasoning_content": "r"}),
        ({"content": "x"}, {"tool_calls": [tc(0, "c", "f", "{}")]}),
    ):
        res = await run(first, later, 6)
        assert res.ttft_ms == 2000.0 and res.decode_tps == 3.0
    two = await run({"content": "x"}, {"content": "y"}, 2)
    assert two.decode_tps == 1.0  # two tokens already count as a rate
    one = await run({"content": "x"}, {"content": "y"}, 1)
    assert one.decode_tps == 0.0


async def test_yes_no_counters_accumulate_and_a_leading_x_is_not_stripped() -> None:
    e, _ = make({CHAT: verdict("YES")})
    await e.yes_no("judge", [Message("user", "q")])
    await e.yes_no("judge", [Message("user", "q")])
    assert e.stats().requests == 2
    bad, _ = make({CHAT: (500, b"{}")})
    for _ in range(2):
        with pytest.raises(EngineError):
            await bad.yes_no("judge", [Message("user", "q")])
    assert bad.stats().errors == 2
    odd, _ = make({CHAT: verdict("xyes")})
    with pytest.raises(EngineError):
        await odd.yes_no("judge", [Message("user", "q")])


def test_finish_passes_known_values_through_unchanged() -> None:
    assert [_finish(x) for x in ("stop", "length", "tool_calls", "cancelled", "other")] == [
        "stop",
        "length",
        "tool_calls",
        "cancelled",
        "stop",
    ]


def test_thinking_off_is_said_at_the_top_level_too_so_a_server_default_budget_leaves_the_reply_alone() -> (
    None
):
    """AIR-7 layer 02, 2026-10-18: the engine ran with --default-thinking-token-budget 2048 from before the
    coder stopped thinking. vllm-mlx gates its budget processor on the request's top-level enable_thinking
    (default True), not on chat_template_kwargs, so the non-thinking coder's replies were cut at 2,048 tokens
    with a </think> stamped in: two test files ended in `assert</think>`."""
    e, _ = make(spec=role_spec(thinking=False))
    off = e._body(req(), stream=False)
    assert off["enable_thinking"] is False and off["chat_template_kwargs"]["enable_thinking"] is False
    on, _ = make(spec=role_spec(thinking=True, thinking_budget=100))
    body = on._body(req(), stream=False)
    assert body["enable_thinking"] is True and body["chat_template_kwargs"]["enable_thinking"] is True
    forced = e._body(req(thinking=True), stream=False)
    assert forced["enable_thinking"] is True
