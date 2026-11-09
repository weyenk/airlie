"""HTTPEngine against an mlx_vlm engine: built the way production builds it, through cli._http_engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from airlie import cli
from airlie.config import load_config
from airlie.engine import EngineError, GenRequest, TokenEvent, ToolSchema
from airlie.engine.http_engine import HTTPEngine
from tests.unit.engine_contract import plain_request

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mlx_vlm"

ROLES = ("coder", "summarizer", "judge", "aggregator")


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


def config(tmp_path: Path):  # type: ignore[no-untyped-def]
    data = {
        "version": 1,
        "engines": {"main": {"kind": "mlx_vlm", "port": 8094, "model": "small"}},
        "models": {
            "small": {
                "repo": "mlx-community/Qwen3-4B-4bit",
                "revision": "r",
                "path": "/models/small",
                "tool_call_parser": "qwen3_coder",
            }
        },
        "roles": {r: {"prompt": f"{r}.md", "model": "small", "thinking": False} for r in ROLES},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    for r in ROLES:
        (tmp_path / f"{r}.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


async def test_request_body_speaks_mlx_vlm(tmp_path: Path) -> None:
    rec = Recorder({"stream": (FIX / "tool_call.sse").read_bytes()})
    eng = cli._http_engine(config(tmp_path), "main", transport=httpx.MockTransport(rec))
    assert isinstance(eng, HTTPEngine)
    req = plain_request()
    req.tools = [
        ToolSchema("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})
    ]
    await eng.generate(req)
    body = json.loads(rec.requests[0].content)
    assert body["model"] == "/models/small"  # the exact --model path, the string the fixtures carry
    assert "chat_template_kwargs" not in body and "thinking_token_budget" not in body
    assert body["enable_thinking"] is False and body["stream_options"] == {"include_usage": True}
    assert body["tool_parser"] == "qwen3_coder"  # the tool_call request


async def test_a_request_without_tools_names_no_tool_parser(tmp_path: Path) -> None:
    rec = Recorder({"stream": (FIX / "plain.sse").read_bytes()})
    eng = cli._http_engine(config(tmp_path), "main", transport=httpx.MockTransport(rec))
    await eng.generate(plain_request())
    assert "tool_parser" not in json.loads(rec.requests[0].content)


async def test_yes_no_speaks_mlx_vlm(tmp_path: Path) -> None:
    reply = json.dumps({"choices": [{"message": {"role": "assistant", "content": "YES"}}]}).encode()
    rec = Recorder({"nonstream": reply})
    eng = cli._http_engine(config(tmp_path), "main", transport=httpx.MockTransport(rec))
    assert await eng.yes_no("judge", plain_request().messages) == (1.0, 0.0)
    body = json.loads(rec.requests[0].content)
    assert body["model"] == "/models/small" and body["enable_thinking"] is False
    assert "chat_template_kwargs" not in body and "thinking_budget" not in body


def _engine(tmp_path: Path, rec: Recorder) -> HTTPEngine:
    eng = cli._http_engine(config(tmp_path), "main", transport=httpx.MockTransport(rec))
    assert isinstance(eng, HTTPEngine)
    return eng


def _tool_request() -> GenRequest:
    req = plain_request()
    req.tools = [
        ToolSchema("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})
    ]
    return req


def _sse(frames: list[dict[str, Any]]) -> bytes:
    return ("".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n").encode()


async def test_reasoning_stream_splits_and_is_not_doubled(tmp_path: Path) -> None:
    eng = _engine(tmp_path, Recorder({"stream": (FIX / "reasoning.sse").read_bytes()}))
    seen: list[TokenEvent] = []
    req = plain_request()
    req.thinking = True
    res = await eng.generate(req, on_token=seen.append)  # the fixture carries reasoning_content and reasoning
    assert res.reasoning and res.text and any(e.reasoning for e in seen)
    assert res.reasoning == "".join(e.text for e in seen if e.reasoning)  # once per delta, not twice
    wire = [
        json.loads(line[6:])
        for line in (FIX / "reasoning.sse").read_text().splitlines()
        if line.startswith("data: {")
    ]
    assert res.reasoning == "".join(
        ch["delta"].get("reasoning_content") or "" for f in wire for ch in f["choices"]
    )


async def test_the_reasoning_alias_alone_is_read(tmp_path: Path) -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": None, "reasoning": "hmm"}}]},
        {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]},
    ]
    eng = _engine(tmp_path, Recorder({"stream": _sse(frames)}))
    res = await eng.generate(plain_request())
    assert res.reasoning == "hmm" and res.text == "ok"


async def test_usage_counters_are_read_from_the_final_chunk(tmp_path: Path) -> None:
    eng = _engine(tmp_path, Recorder({"stream": (FIX / "resume.sse").read_bytes()}))
    res = await eng.generate(plain_request())
    assert res.cached_tokens == 16  # usage.prompt_tokens_details.cached_tokens in resume.sse
    assert res.prompt_tokens == 49 and res.completion_tokens == 3
    assert res.draft_tokens is None and res.accepted_draft_tokens is None  # timings.draft_n is null
    assert res.ttft_ms > 0  # measured by the client


async def test_draft_counters_are_read_from_timings(tmp_path: Path) -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 40, "prompt_tokens_details": None},
            "timings": {"draft_n": 40, "draft_n_accepted": 31},
        },
    ]
    eng = _engine(tmp_path, Recorder({"stream": _sse(frames)}))
    res = await eng.generate(plain_request())
    assert res.draft_tokens == 40 and res.accepted_draft_tokens == 31
    assert res.cached_tokens is None and res.completion_tokens == 40


async def test_null_fields_are_tolerated(tmp_path: Path) -> None:
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
    eng = _engine(tmp_path, Recorder({"stream": _sse(frames)}))
    res = await eng.generate(_tool_request())
    assert [c.name for c in res.tool_calls] == ["read_file"] and res.prompt_tokens == 0


async def test_tool_call_stream_assembles_by_index(tmp_path: Path) -> None:
    eng = _engine(tmp_path, Recorder({"stream": (FIX / "tool_call.sse").read_bytes()}))
    res = await eng.generate(_tool_request())
    assert [c.name for c in res.tool_calls] == ["read_file"]
    assert json.loads(res.tool_calls[0].arguments)


async def test_raw_output_is_left_as_the_server_sent_it(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.models["small"].tool_call_parser = "atem"  # the server parses; airlie does not read the name here
    rec = Recorder({"stream": (FIX / "plain.sse").read_bytes()})
    eng = cli._http_engine(cfg, "main", transport=httpx.MockTransport(rec))
    res = await eng.generate(plain_request())
    assert res.text and res.tool_calls == []  # unchanged


async def test_health_reads_loaded_models(tmp_path: Path) -> None:
    rec = Recorder(
        {"/health": (FIX / "health.json").read_bytes(), "/v1/models": (FIX / "models.json").read_bytes()}
    )
    h = await _engine(tmp_path, rec).health()
    assert h.ok and h.models["/models/small"] == "ready"
    assert all(s == "lazy" for k, s in h.models.items() if k != "/models/small")  # listed, not loaded
    assert "loaded_model=/models/small" in h.detail


async def test_an_error_status_raises_with_the_servers_detail(tmp_path: Path) -> None:
    body = (FIX / "error_bad_model.json").read_bytes()
    eng = _engine(tmp_path, Recorder({"stream": (400, body)}))
    with pytest.raises(EngineError) as exc:
        await eng.generate(plain_request())
    assert str(json.loads(body)["detail"]) in str(exc.value)
