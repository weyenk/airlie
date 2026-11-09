"""HTTPEngine against an mlx_vlm engine: built the way production builds it, through cli._http_engine."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

from airlie import cli
from airlie.config import load_config
from airlie.engine import ToolSchema
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
