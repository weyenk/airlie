"""RoutedEngine, and cli._http_engine building one when roles live on different engines."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import yaml

from airlie import cli
from airlie.config import load_config
from airlie.engine import EngineCapabilities, EngineHealth, GenRequest, GenResult, Message
from airlie.engine.http_engine import HTTPEngine
from airlie.engine.routed import RoutedEngine
from airlie.engine.types import EngineStats
from tests.unit.engine_contract import plain_request

ROLES = ("coder", "summarizer", "judge", "aggregator", "reviewer")


def config(tmp_path: Path, *, reviewer_engine: bool = True):  # type: ignore[no-untyped-def]
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "mlx_vlm", "port": 8080, "model": "generalist"},
            **(
                {"reviewer": {"kind": "mlx_vlm", "port": 8082, "model": "reviewer"}}
                if reviewer_engine
                else {}
            ),
        },
        "models": {
            "generalist": {"repo": "org/g", "revision": "r", "path": "/models/generalist"},
            "reviewer": {"repo": "org/r", "revision": "r", "path": "/models/reviewer"},
        },
        "roles": {
            r: {
                "prompt": f"{r}.md",
                "model": "reviewer" if r == "reviewer" and reviewer_engine else "generalist",
            }
            for r in ROLES
        },
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    for r in ROLES:
        (tmp_path / f"{r}.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


async def test_each_role_goes_to_the_engine_that_serves_its_model(tmp_path: Path) -> None:
    seen: list[tuple[str, dict]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": str(req.url.port)}]})
        seen.append((str(req.url), json.loads(req.content)))
        sse = (
            b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        )
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    eng = cli._http_engine(config(tmp_path), "main", transport=httpx.MockTransport(handle))
    assert isinstance(eng, RoutedEngine)
    await eng.start()
    await eng.generate(plain_request("reviewer"))
    await eng.generate(plain_request("coder"))
    assert [(u, b["model"]) for u, b in seen] == [
        ("http://127.0.0.1:8082/v1/chat/completions", "/models/reviewer"),
        ("http://127.0.0.1:8080/v1/chat/completions", "/models/generalist"),
    ]
    assert list((await eng.health()).models) == ["8080"]  # main's
    assert eng.stats().requests == 2
    await eng.stop()


def test_one_engine_serving_every_role_stays_a_plain_http_engine(tmp_path: Path) -> None:
    eng = cli._http_engine(config(tmp_path, reviewer_engine=False), "main")
    assert isinstance(eng, HTTPEngine)


def test_a_model_override_is_one_http_engine_even_with_roles_elsewhere(tmp_path: Path) -> None:
    eng = cli._http_engine(config(tmp_path), "main", "generalist")
    assert isinstance(eng, HTTPEngine)


class Fake:
    capabilities = EngineCapabilities(batching=True, prefix_cache=True, logprobs=False, native_tools=True)

    def __init__(self, name: str) -> None:
        self.name = name
        self.cancelled: list[str] = []
        self.started = self.stopped = False
        self.release = asyncio.Event()
        self.n = 1

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def health(self) -> EngineHealth:
        return EngineHealth(ok=True, models={}, detail=self.name)

    async def generate(self, req: GenRequest, on_token=None) -> GenResult:  # type: ignore[no-untyped-def]
        await self.release.wait()
        return GenResult(self.name, "", [], "stop", 0, 0)

    async def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)

    async def yes_no(self, role: str, messages: list[Message]) -> tuple[float, float]:
        return (1.0, 0.0) if self.name == "a" else (0.0, 1.0)

    def stats(self) -> EngineStats:
        return EngineStats(requests=self.n, tokens_out=10 * self.n, errors=self.n - 1)


def routed() -> tuple[RoutedEngine, Fake, Fake]:
    a, b = Fake("a"), Fake("b")
    b.n = 3
    return RoutedEngine("a", {"a": a, "b": b}, lambda role: "b" if role == "rev" else "a"), a, b


async def test_cancel_reaches_the_engine_that_took_the_request() -> None:
    eng, a, b = routed()
    req = plain_request("coder")
    task = asyncio.create_task(eng.generate(req))
    await asyncio.sleep(0)
    await eng.cancel(req.request_id)
    assert (a.cancelled, b.cancelled) == ([req.request_id], [])
    a.release.set()
    assert (await task).text == "a"
    await eng.cancel("unknown")  # not in flight: every engine hears it
    assert (a.cancelled[-1], b.cancelled) == ("unknown", ["unknown"])


async def test_yes_no_start_stop_health_and_stats() -> None:
    eng, a, b = routed()
    assert await eng.yes_no("coder", []) == (1.0, 0.0)
    assert await eng.yes_no("rev", []) == (0.0, 1.0)
    await eng.start()
    await eng.stop()
    assert a.started and b.started and a.stopped and b.stopped
    assert (await eng.health()).detail == "a"
    assert eng.stats() == EngineStats(requests=4, tokens_out=40, errors=2)
    assert eng.capabilities is a.capabilities
