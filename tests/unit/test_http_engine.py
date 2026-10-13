"""HTTPEngine against the recorded vllm-mlx fixtures, replayed by httpx.MockTransport."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from airlie.config import RoleSpec
from airlie.engine import EngineError, Message, TokenEvent, ToolSchema
from airlie.engine.http_engine import HTTPEngine
from tests.unit.engine_contract import check_plain_stream, plain_request

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "vllm_mlx"


def role_spec(prompt: Path = Path("/dev/null")) -> RoleSpec:
    return RoleSpec(prompt=prompt, model="small", thinking=False, thinking_budget=None, max_tokens=64)


class Recorder:
    """MockTransport handler that serves fixtures and records request bodies."""

    def __init__(self, routes: dict[str, bytes | tuple[int, bytes]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path
        if key == "/v1/chat/completions":
            body = json.loads(request.content)
            key = "stream" if body.get("stream") else "nonstream"
        resp = self.routes[key]
        status, content = resp if isinstance(resp, tuple) else (200, resp)
        headers = (
            {"content-type": "text/event-stream"} if key == "stream" else {"content-type": "application/json"}
        )
        return httpx.Response(status, content=content, headers=headers)


def engine(routes: dict[str, bytes | tuple[int, bytes]]) -> tuple[HTTPEngine, Recorder]:
    rec = Recorder(routes)
    eng = HTTPEngine(
        base_url="http://engine.test/v1",
        served_name_for_role=lambda role: "small",
        role_spec_for=lambda role: role_spec(),
        transport=httpx.MockTransport(rec),
    )
    return eng, rec


def sse(name: str) -> bytes:
    return (FIX / f"{name}.sse").read_bytes()


async def test_plain_stream_contract() -> None:
    eng, rec = engine({"stream": sse("plain")})
    await check_plain_stream(eng, plain_request(), "red")
    body = json.loads(rec.requests[0].content)
    assert body["model"] == "small"
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 32
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0] == {"role": "system", "content": "Reply in one short sentence."}


async def test_usage_comes_from_the_final_usage_frame() -> None:
    eng, _ = engine({"stream": sse("plain")})
    res = await eng.generate(plain_request())
    assert (res.prompt_tokens, res.completion_tokens) == (28, 13)
    assert res.cached_tokens is None  # vllm-mlx 0.5.0 does not report prompt_tokens_details


async def test_reasoning_deltas_are_separated_from_content() -> None:
    eng, rec = engine({"stream": sse("reasoning")})
    seen: list[TokenEvent] = []
    req = plain_request()
    req.thinking = True
    res = await eng.generate(req, on_token=seen.append)
    assert seen[0].reasoning is True
    assert res.reasoning.startswith("\nOkay")
    assert "Okay" not in res.text
    assert json.loads(rec.requests[0].content)["chat_template_kwargs"] == {"enable_thinking": True}


async def test_tool_calls_are_merged_by_index_and_finish_is_tool_calls() -> None:
    eng, rec = engine({"stream": sse("tool_call")})
    req = plain_request()
    req.tools = [
        ToolSchema("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})
    ]
    res = await eng.generate(req)
    assert res.finish_reason == "tool_calls"
    assert len(res.tool_calls) == 1  # vllm-mlx emits the same index twice; merge, don't duplicate
    assert res.tool_calls[0].name == "read_file"
    assert json.loads(res.tool_calls[0].arguments) == {"path": "README.md"}
    body = json.loads(rec.requests[0].content)
    assert body["tools"][0]["function"]["name"] == "read_file"
    assert body["tool_choice"] == "auto"


async def test_fastapi_detail_error_becomes_engine_error() -> None:
    eng, _ = engine({"stream": (404, (FIX / "error_bad_model.json").read_bytes())})
    with pytest.raises(EngineError, match=r"404.*does not exist"):
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


async def test_truncated_stream_raises() -> None:
    eng, _ = engine(
        {"stream": b'data: {"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n\n'}
    )
    with pytest.raises(EngineError, match=r"without \[DONE\]"):
        await eng.generate(plain_request())


async def test_yes_no_falls_back_to_constrained_text_answer() -> None:
    eng, rec = engine({"nonstream": (FIX / "logprobs.json").read_bytes()})
    p_yes, p_no = await eng.yes_no("judge", [Message(role="user", content="Is water wet?")])
    assert (p_yes, p_no) == (1.0, 0.0)
    body = json.loads(rec.requests[0].content)
    assert body["max_tokens"] <= 4 and body["stream"] is False
    assert eng.capabilities.logprobs is False


async def test_health_reads_available_models() -> None:
    eng, _ = engine(
        {"/health": (FIX / "health.json").read_bytes(), "/v1/models": (FIX / "models.json").read_bytes()}
    )
    h = await eng.health()
    assert h.ok is True
    assert h.models == {"small": "ready"}


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
    blocks) until max_tokens; a mild mlx-lm style repetition_penalty is the per-request guard."""
    from airlie.config import RoleSpec, Sampling
    from airlie.engine.http_engine import HTTPEngine
    from airlie.engine.types import GenRequest, Message

    role = RoleSpec(prompt=Path(__file__), model="base", thinking=False, max_tokens=64)
    eng = HTTPEngine("http://x/v1", served_name_for_role=lambda r: "base", role_spec_for=lambda r: role)
    req = GenRequest(
        role="coder", messages=[Message(role="user", content="hi")], sampling=Sampling(), max_tokens=10
    )
    assert "repetition_penalty" not in eng._body(req, stream=False)  # noqa: SLF001 - 1.0 means off
    req.sampling = Sampling(repetition_penalty=1.05)
    assert eng._body(req, stream=False)["repetition_penalty"] == 1.05  # noqa: SLF001


async def test_engine_applies_the_models_parsers_to_raw_content() -> None:
    from airlie.config import ModelSpec

    raw = (Path(__file__).resolve().parents[1] / "fixtures" / "parsers" / "qwen3_coder_xml.txt").read_text()
    frames = [
        {"choices": [{"delta": {"content": raw}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 9},
        },
    ]
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
    rec = Recorder({"stream": body.encode()})
    eng = HTTPEngine(
        base_url="http://engine.test/v1",
        served_name_for_role=lambda role: "base",
        role_spec_for=lambda role: role_spec(),
        model_spec_for=lambda role: ModelSpec(repo="o/m", revision="r", tool_call_parser="qwen3_coder"),
        transport=httpx.MockTransport(rec),
    )
    res = await eng.generate(plain_request())
    assert res.finish_reason == "tool_calls" and res.tool_calls[0].name == "read_file"
