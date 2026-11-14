"""The reviewer and the supervisor reach an mlx_vlm engine with their reasoning effort: built the way the
studio review step builds the request (Reviewer over cli._http_engine, MockTransport capturing the body)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from airlie import cli
from airlie.config import load_config
from airlie.engine.http_engine import HTTPEngine
from airlie.topology.review import Reviewer

ROLES = ("reviewer", "supervisor", "architect", "summarizer", "judge", "aggregator")
ROLE = {"model": "muse", "thinking": False, "chat_template_kwargs": {"reasoning_strength": "low"}}


def _config(tmp_path: Path) -> Any:
    data = {
        "version": 1,
        "engines": {"main": {"kind": "mlx_vlm", "port": 8094, "model": "muse"}},
        "models": {"muse": {"repo": "mlx-community/muse", "revision": "r", "path": "/models/muse"}},
        "roles": {r: {"prompt": f"{r}.md", **ROLE} for r in ROLES},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    for r in ROLES:
        (tmp_path / f"{r}.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def _sse(text: str) -> bytes:
    chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}]}
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


@pytest.mark.parametrize("role", ["reviewer", "supervisor"])
async def test_the_review_request_carries_reasoning_effort_and_thinking(tmp_path: Path, role: str) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_sse("verdict"), headers={"content-type": "text/event-stream"})

    cfg = _config(tmp_path)
    eng = cli._http_engine(cfg, "main", transport=httpx.MockTransport(handler))
    assert isinstance(eng, HTTPEngine)
    await Reviewer(eng, role, cfg.roles[role]).review("brief", "diff", "tests", {})
    body = bodies[0]
    assert body["reasoning_effort"] == "low"
    assert body["enable_thinking"] is False
    assert "chat_template_kwargs" not in body
