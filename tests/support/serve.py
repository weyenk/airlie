"""Shared helpers extracted from tests/unit/test_serve.py for tests.support.serve."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

import httpx
import yaml

from airlie.config import load_config
from airlie.engine.types import EngineError
from airlie.manager.core import Manager, Report
from airlie.models_download import DownloadProgress
from airlie.serve.bridge.clock import FakeClock


def cfg(tmp_path: Path):
    for r in ("architect", "summarizer", "judge", "aggregator"):
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
            "architect": {"prompt": "architect.md", "model": "base"},
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


class FakeEngine:
    """A manager resource that starts at once (or fails); `up` is what a health probe would say."""

    kind: Literal["engine", "vm"] = "engine"
    lifecycle: Literal["pinned", "on_demand"] = "on_demand"
    idle_unload_s = 600.0
    footprint_gb = 10.0

    def __init__(self, name: str = "main") -> None:
        self.name = name
        self.up = False
        self.fail: str | None = None
        self.progress: DownloadProgress | None = None
        self.gate: asyncio.Event | None = None  # when set, start waits on it after reporting its progress
        self.reported = asyncio.Event()

    async def running(self) -> bool:
        return self.up

    async def start(self, report: Report) -> None:
        if self.progress is not None:
            report("downloading", "org/m", self.progress)
        self.reported.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise EngineError(self.fail)
        self.up = True

    async def stop(self) -> None:
        self.up = False


class StuckClock(FakeClock):
    """A clock whose sleeps never finish: the reap loop and waiters move only when a test says so."""

    async def sleep(self, seconds: float) -> None:
        await asyncio.Event().wait()


def unmanaged() -> Manager:
    """A manager over no engines: serve proxies to whatever is already listening, as before the manager."""
    return Manager([], budget_gb=0.0, clock=FakeClock())
