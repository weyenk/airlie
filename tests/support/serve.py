"""Shared helpers extracted from tests/unit/test_serve.py for tests.support.serve."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

from airlie.config import load_config


def cfg(tmp_path: Path):
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (tmp_path / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {
            "main": {
                "kind": "openai_http",
                "base_url": "http://engine.test/v1",
                "models": ["base", "small"],
                "served_names": {"small": "tiny"},
            }
        },
        "models": {"base": {"repo": "org/m", "revision": "r"}, "small": {"repo": "org/s", "revision": "r"}},
        "roles": {
            "coder": {"prompt": "coder.md", "model": "base"},
            "summarizer": {"prompt": "summarizer.md", "model": "small"},
            "judge": {"prompt": "judge.md", "model": "small"},
            "aggregator": {"prompt": "aggregator.md", "model": "base"},
        },
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


class FakeUpstream:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if body.get("stream"):
            sse = (
                b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
            )
            return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": body["model"],
            },
        )
