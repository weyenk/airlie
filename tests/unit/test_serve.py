"""task serve: OpenAI-compatible front door. /v1/models lists roles; chat completions proxy to the engine with
the role→served-model mapping; /v1/airlie/run launches a team run."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml
from fastapi.testclient import TestClient

from airlie.config import load_config
from airlie.obs import RunDir
from airlie.orchestrator import RunReport
from airlie.serve.app import create_app
from airlie.teams import TeamResult


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


def test_models_lists_roles(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    r = TestClient(app).get("/v1/models")
    assert r.status_code == 200
    assert sorted(m["id"] for m in r.json()["data"]) == ["aggregator", "coder", "judge", "summarizer"]


def test_chat_completion_maps_role_to_served_model_and_prepends_system_prompt(tmp_path: Path) -> None:
    up = FakeUpstream()
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(up))
    r = TestClient(app).post(
        "/v1/chat/completions", json={"model": "summarizer", "messages": [{"role": "user", "content": "yo"}]}
    )
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hi"
    sent = json.loads(up.requests[0].content)
    assert sent["model"] == "tiny"  # served_names mapping
    assert sent["messages"][0] == {"role": "system", "content": "x"}  # role prompt prepended
    assert str(up.requests[0].url).startswith("http://engine.test/v1/chat/completions")


def test_chat_completion_streams_through(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "coder", "stream": True, "messages": [{"role": "user", "content": "yo"}]},
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes())
    assert b'"content":"hi"' in body and b"[DONE]" in body


def test_unknown_role_is_404(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    r = TestClient(app).post("/v1/chat/completions", json={"model": "ghost", "messages": []})
    assert r.status_code == 404 and "ghost" in r.json()["detail"]


def test_run_endpoint_uses_orchestrator_factory(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    class Stub:
        def __init__(self) -> None:
            self.run_dir = RunDir.create(c.observability.state_dir, "s1")

        async def execute_task(self, goal, *, repo, test_cmd=None):
            return RunReport(
                "s1",
                self.run_dir,
                goal,
                [TeamResult("team-1", True, "done", "did", 1, 1, 1, 10.0)],
                20.0,
                winner="team-1",
                final_answer="did it",
                scores={"team-1": 1.0},
            )

    app = create_app(
        c, transport=httpx.MockTransport(FakeUpstream()), orchestrator_factory=lambda cfg, k: Stub()
    )
    r = TestClient(app).post("/v1/airlie/run", json={"goal": "g", "repo": str(repo), "k": 1})
    assert r.status_code == 200
    j = r.json()
    assert j["winner"] == "team-1" and j["final_answer"] == "did it" and j["teams"][0]["team_id"] == "team-1"


def test_run_endpoint_passes_topology_to_factory(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    seen: list[str] = []

    class Stub:
        def __init__(self) -> None:
            self.run_dir = RunDir.create(c.observability.state_dir, "s2")

        async def execute_task(self, goal, *, repo, test_cmd=None):
            return RunReport("s2", self.run_dir, goal, [], 1.0, winner=None, final_answer="")

    def factory(cfg, k, topology=None):  # type: ignore[no-untyped-def]
        seen.append(topology)
        return Stub()

    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()), orchestrator_factory=factory)
    r = TestClient(app).post("/v1/airlie/run", json={"goal": "g", "repo": str(repo), "topology": "fork"})
    assert r.status_code == 200, r.text
    assert seen == ["fork"]


def test_run_endpoint_rejects_unknown_topology_with_400(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()))
    r = TestClient(app).post("/v1/airlie/run", json={"goal": "g", "repo": str(repo), "topology": "ring"})
    assert r.status_code == 400 and "ring" in r.json()["detail"]


def test_run_endpoint_accepts_single(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    seen: list[tuple[str, int, str | None]] = []

    class Stub:
        def __init__(self) -> None:
            self.run_dir = RunDir.create(c.observability.state_dir, "s3")

        async def execute_task(self, goal, *, repo, test_cmd=None):
            return RunReport("s3", self.run_dir, goal, [], 1.0, winner=None, final_answer="")

    def factory(cfg, k, topology=None):  # type: ignore[no-untyped-def]
        seen.append((cfg.topology.kind, cfg.teams.k, topology))
        return Stub()

    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()), orchestrator_factory=factory)
    r = TestClient(app).post("/v1/airlie/run", json={"goal": "g", "repo": str(repo), "topology": "single"})
    assert r.status_code == 200, r.text
    assert seen[0][2] == "single"  # a custom factory gets the raw cfg + name; _default_factory selects (next test)


def test_default_factory_selects_single_profile(tmp_path: Path) -> None:
    from airlie.serve.app import _default_factory

    c = cfg(tmp_path)
    captured: list = []

    import airlie.cli as cli_mod

    original = cli_mod._orchestrator
    cli_mod._orchestrator = lambda cfg, run_id=None: captured.append(cfg)  # type: ignore[assignment]
    try:
        _default_factory(c, None, topology="single")
    finally:
        cli_mod._orchestrator = original
    assert captured[0].topology.kind == "single"
