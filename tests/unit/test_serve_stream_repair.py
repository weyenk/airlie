"""Serve-side OpenAI-compatible SSE repair for mlx-vlm → @ai-sdk/openai-compatible clients."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from airlie.serve.app import ToolCallStreamRepair, create_app
from tests.support.serve import cfg
from tests.unit.serve_helpers import TestClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "serve"


def _parsed_sse(text: str) -> list[dict[str, object]]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]


def test_a_string_engine_error_becomes_an_openai_error_object() -> None:
    raw = FIXTURES.joinpath("architect-engine-error.sse").read_bytes().split(b"\n\n", 1)[0]
    out = ToolCallStreamRepair().frame(raw)
    baked = json.loads(out.decode()[6:])
    assert baked["error"] == {
        "message": (
            "QSAKVCache does not support quantized continuous batching with model-specific cache state; "
            "disable KV quantization for continuous batching"
        ),
        "type": "server_error",
    }


def test_typed_content_parts_become_string_content_and_reasoning_content() -> None:
    frame = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "content": [
                        {"type": "thinking", "thinking": [{"type": "text", "text": "hm"}]},
                        {"type": "text", "text": "Hi"},
                    ]
                },
                "finish_reason": None,
            }
        ]
    }
    out = json.loads(ToolCallStreamRepair().frame(f"data: {json.dumps(frame)}".encode()).decode()[6:])
    delta = out["choices"][0]["delta"]
    assert delta["content"] == "Hi"
    assert delta["reasoning_content"] == "hm"


class StringErrorUpstream:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = FIXTURES.joinpath("architect-engine-error.sse").read_bytes()
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def test_streamed_string_errors_reach_clients_as_openai_errors(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(StringErrorUpstream()))
    body = {"model": "architect", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(app) as c:
        text = c.post("/v1/chat/completions", json=body).text
    [err] = _parsed_sse(text)
    assert err["error"]["message"].startswith("QSAKVCache") and err["error"]["type"] == "server_error"


@pytest.mark.parametrize(
    "reasoning",
    [42, {"tokens": ["x"]}],
    ids=["int", "object"],
)
def test_non_string_reasoning_fields_are_coerced_to_strings(reasoning: object) -> None:
    frame = {
        "choices": [
            {"index": 0, "delta": {"content": "x", "reasoning_content": reasoning}, "finish_reason": None}
        ]
    }
    out = json.loads(ToolCallStreamRepair().frame(f"data: {json.dumps(frame)}".encode()).decode()[6:])
    assert isinstance(out["choices"][0]["delta"]["reasoning_content"], str)
