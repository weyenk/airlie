"""web_search tool against a SearXNG provider: fake client is the only transport, run-dir logging."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from airlie.tools.protocol import ToolContext
from airlie.tools.web import SearXNGProvider, WebSearch

BASE_URL = "http://127.0.0.1:8888"


@dataclass
class FakeResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


@dataclass
class FakeClient:
    """A WebClient double: the only transport; records every request it is asked to make."""

    responses: list[FakeResponse] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def request(self, url: str, timeout_s: float) -> FakeResponse:
        self.requests.append(url)
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


def search_ctx(tmp_path: Path) -> ToolContext:
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    return ToolContext(worktree=wt, run_dir=run_dir, role="research")


def searxng_json() -> bytes:
    return json.dumps(
        {
            "results": [
                {
                    "url": "https://docs.vllm.ai/mlx",
                    "title": "vLLM on MLX",
                    "content": "how to run vllm on mlx",
                },
                {"url": "https://example.org/mlx", "title": "MLX notes", "content": "mlx notes"},
            ]
        }
    ).encode()


# ---- AC9: a 200 JSON response returns the results and logs an ok line ----


async def test_search_returns_results_and_logs_ok_line(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[
            FakeResponse(200, {"content-type": "application/json"}, searxng_json(), BASE_URL + "/search")
        ]
    )
    tool = WebSearch(SearXNGProvider(BASE_URL, client))
    r = await tool.run({"query": "vllm mlx"}, search_ctx(tmp_path))
    assert r.ok
    assert "vLLM on MLX" in r.output and "https://docs.vllm.ai/mlx" in r.output
    log = tmp_path / "run" / "web_search.jsonl"
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["role"] == "research"
    assert rec["query"] == "vllm mlx"
    assert rec["status"] == "ok"


# ---- AC10: an HTTP 500 from the backend fails with 'search failed' and logs an error line ----


async def test_backend_500_fails_and_logs_error_line(tmp_path: Path) -> None:
    client = FakeClient(responses=[FakeResponse(500, {}, b"boom", BASE_URL + "/search")])
    tool = WebSearch(SearXNGProvider(BASE_URL, client))
    r = await tool.run({"query": "vllm mlx"}, search_ctx(tmp_path))
    assert not r.ok and "search failed" in r.output
    log = tmp_path / "run" / "web_search.jsonl"
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["role"] == "research"
    assert rec["query"] == "vllm mlx"
    assert rec["status"] == "error"


# ---- AC11: the request goes to the configured SearXNG base URL; the fake is the only transport ----


async def test_request_goes_to_configured_searxng_base_url(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[
            FakeResponse(200, {"content-type": "application/json"}, searxng_json(), BASE_URL + "/search")
        ]
    )
    tool = WebSearch(SearXNGProvider(BASE_URL, client))
    r = await tool.run({"query": "vllm mlx"}, search_ctx(tmp_path))
    assert r.ok
    assert client.requests == [f"{BASE_URL}/search?q=vllm+mlx&format=json"]
