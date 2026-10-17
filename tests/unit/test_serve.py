"""task serve: OpenAI-compatible front door. /v1/models lists roles; chat completions proxy to the engine with
the role→served-model mapping; /v1/airlie/run launches a team run."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from airlie.config import load_config
from airlie.obs import RunDir
from airlie.orchestrator import RunReport
from airlie.serve.app import create_app
from airlie.teams import TeamResult
from tests.unit.serve_helpers import TestClient
from tests.unit.vllm_helpers import ChatCompletionRequest


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
    ids = [m["id"] for m in r.json()["data"]]
    assert sorted(ids[:4]) == ["aggregator", "coder", "judge", "summarizer"]  # roles first
    assert sorted(ids[4:]) == ["base", "small"]  # then the raw catalog models


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


class CutOffUpstream:
    """An engine whose reply reached max_tokens while the model was still thinking: no content and a
    finish_reason of "length" (HDS-3, 2026-10-15)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if body.get("stream"):
            sse = (
                b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"Let me think"},'
                b'"finish_reason":null}]}\n\n'
                b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":4096}}\n\n'
                b"data: [DONE]\n\n"
            )
            return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
        message = {"role": "assistant", "content": "", "reasoning_content": "Let me think"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 4096},
                "model": body["model"],
            },
        )


def test_a_reply_cut_off_at_max_tokens_is_returned_with_finish_reason_length_not_an_error(
    tmp_path: Path,
) -> None:
    up, c = CutOffUpstream(), cfg(tmp_path)
    app = create_app(c, transport=httpx.MockTransport(up))
    r = TestClient(app).post(
        "/v1/chat/completions", json={"model": "coder", "messages": [{"role": "user", "content": "yo"}]}
    )
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "length" and choice["message"]["content"] == ""
    assert choice["message"]["reasoning_content"] == "Let me think"  # what the engine sent is passed on
    assert json.loads(up.requests[0].content)["max_tokens"] == c.roles["coder"].max_tokens  # the role's own


def test_a_streamed_reply_cut_off_at_max_tokens_ends_with_finish_reason_length(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(CutOffUpstream()))
    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "coder", "stream": True, "messages": [{"role": "user", "content": "yo"}]},
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes())
    assert b'"finish_reason":"length"' in body and b"[DONE]" in body


def test_a_role_that_thinks_and_has_a_budget_is_sent_to_the_engine_as_thinking_token_budget(
    tmp_path: Path,
) -> None:
    """The name vllm-mlx reads (it drops others, `thinking_budget` among them); HTTPEngine sends the same."""
    up, c = FakeUpstream(), cfg(tmp_path)
    c.roles["coder"].thinking = False  # does not think: no budget to send
    c.roles["summarizer"].thinking_budget = None  # thinks, uncapped: none either
    client = TestClient(create_app(c, transport=httpx.MockTransport(up)))
    ask = {"messages": [{"role": "user", "content": "yo"}]}
    # judge thinks with the default budget; base is a raw model, not a role
    for model in ("judge", "coder", "summarizer", "base"):
        assert client.post("/v1/chat/completions", json={"model": model, **ask}).status_code == 200
    bodies = [json.loads(r.content) for r in up.requests]
    sent = [b.get("thinking_token_budget") for b in bodies]
    assert sent == [c.roles["judge"].thinking_budget, None, None, None]
    assert c.roles["judge"].thinking_budget == 8192 and not any("thinking_budget" in b for b in bodies)


def test_vllm_mlx_reads_the_budget_from_the_body_serve_sends(tmp_path: Path) -> None:
    """vllm-mlx's own request model, given the body serve forwards, comes out with the role's budget."""
    up, c = FakeUpstream(), cfg(tmp_path)
    client = TestClient(create_app(c, transport=httpx.MockTransport(up)))
    client.post(
        "/v1/chat/completions", json={"model": "judge", "messages": [{"role": "user", "content": "yo"}]}
    )
    read = ChatCompletionRequest.model_validate(json.loads(up.requests[0].content))
    assert read.thinking_token_budget == c.roles["judge"].thinking_budget == 8192
    assert read.max_tokens == c.roles["judge"].max_tokens


def test_a_clients_own_thinking_token_budget_wins_and_a_stream_gets_the_roles(tmp_path: Path) -> None:
    up = FakeUpstream()
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(up)))
    ask = {"messages": [{"role": "user", "content": "yo"}]}
    client.post("/v1/chat/completions", json={"model": "judge", "thinking_token_budget": 100, **ask})
    with client.stream("POST", "/v1/chat/completions", json={"model": "judge", "stream": True, **ask}) as r:
        assert r.status_code == 200
        b"".join(r.iter_bytes())
    assert [json.loads(x.content)["thinking_token_budget"] for x in up.requests] == [100, 8192]


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


def test_run_endpoint_reports_a_team_cut_off_at_max_tokens_as_not_ok_with_why(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    why = "the reply was cut off at max_tokens (4096) before it ended; raise roles.coder.max_tokens"

    class Stub:
        def __init__(self) -> None:
            self.run_dir = RunDir.create(c.observability.state_dir, "s2")

        async def execute_task(self, goal, *, repo, test_cmd=None):
            cut = TeamResult("team-1", False, "cut_off", why, 0, 1, 4096, 10.0)
            return RunReport("s2", self.run_dir, goal, [cut], 20.0, winner="team-1", final_answer=why)

    app = create_app(
        c, transport=httpx.MockTransport(FakeUpstream()), orchestrator_factory=lambda cfg, k: Stub()
    )
    r = TestClient(app).post("/v1/airlie/run", json={"goal": "g", "repo": str(repo), "k": 1})
    assert r.status_code == 200
    assert r.json()["final_answer"] == why
    assert r.json()["teams"] == [
        {"team_id": "team-1", "ok": False, "stop_reason": "cut_off", "steps": 0, "final_text": why}
    ]


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
    # a custom factory gets the raw cfg + the name; _default_factory does the selection (next test)
    assert seen[0][2] == "single"


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


def test_default_factory_with_k_above_one_selects_teams(tmp_path: Path) -> None:
    from airlie.serve.app import _default_factory

    c = cfg(tmp_path)
    c.topology.kind = "single"  # type: ignore[assignment]
    captured: list = []

    import airlie.cli as cli_mod

    original = cli_mod._orchestrator
    cli_mod._orchestrator = lambda cfg, run_id=None: captured.append(cfg)  # type: ignore[assignment]
    try:
        _default_factory(c, 3)
    finally:
        cli_mod._orchestrator = original
    assert captured[0].topology.kind == "teams" and captured[0].teams.k == 3
    assert c.topology.kind == "single"  # the shared config is not mutated by a request


def test_run_endpoint_accepts_images(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    png = tmp_path / "s.png"
    png.write_bytes(b"\x89PNG")
    seen: list[list[Path]] = []

    class Stub:
        def __init__(self) -> None:
            self.run_dir = RunDir.create(c.observability.state_dir, "s4")

        async def execute_task(self, goal, *, repo, test_cmd=None, images=()):  # type: ignore[no-untyped-def]
            seen.append(list(images))
            return RunReport("s4", self.run_dir, goal, [], 1.0, winner=None, final_answer="")

    app = create_app(
        c, transport=httpx.MockTransport(FakeUpstream()), orchestrator_factory=lambda *a, **k: Stub()
    )
    body = {"goal": "g", "repo": str(repo), "topology": "studio", "images": [str(png)]}
    r = TestClient(app).post("/v1/airlie/run", json=body)
    assert r.status_code == 200, r.text
    assert seen == [[png.resolve()]]


def test_api_key_guards_the_v1_routes_but_not_health(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="s3cret")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/v1/models").status_code == 401
        assert c.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/v1/models", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        body = {"model": "coder", "messages": [{"role": "user", "content": "hi"}]}
        assert c.post("/v1/chat/completions", json=body).status_code == 401
        ok = c.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


def test_raw_catalog_models_are_served_without_a_prompt(tmp_path: Path) -> None:
    up = FakeUpstream()
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(up))
    with TestClient(app) as c:
        ids = {m["id"] for m in c.get("/v1/models").json()["data"]}
        assert {"coder", "small", "base"} <= ids  # roles and raw catalog models
        r = c.post(
            "/v1/chat/completions", json={"model": "small", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert r.status_code == 200
    sent = json.loads(up.requests[-1].content)
    assert (
        sent["model"] == "tiny" and sent["messages"][0]["role"] == "user"
    )  # served name, no prompt prepended


def test_role_prompt_precedes_a_client_system_prompt(tmp_path: Path) -> None:
    up = FakeUpstream()
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(up))
    (tmp_path / "coder.md").write_text("---\nsummary: Implement code\n---\n# coder\nUse write_file.")
    body = {
        "model": "coder",
        "messages": [
            {"role": "system", "content": "You are OpenCode. Tools: write, read, bash."},
            {"role": "user", "content": "hi"},
        ],
    }
    with TestClient(app) as c:
        assert c.post("/v1/chat/completions", json=body).status_code == 200
    sent = json.loads(up.requests[-1].content)["messages"]
    assert [m["role"] for m in sent] == ["system", "system", "user"]
    assert sent[0]["content"].startswith("# coder") and "use the tools offered" in sent[0]["content"]
    assert sent[1]["content"].startswith("You are OpenCode")


class IdlessToolStream:
    """An upstream whose streamed tool-call deltas carry no id/type (what broke OpenCode's AI SDK)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        frames = [
            {"choices": [{"index": 0, "delta": {"content": "I'll look."}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"name": "glob", "arguments": ""}}]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '{"pattern": "*.html"}'}}]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def test_streamed_tool_calls_always_carry_an_id_and_type(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(IdlessToolStream()))
    body = {"model": "base", "stream": True, "messages": [{"role": "user", "content": "find html"}]}
    with TestClient(app) as c:
        text = c.post("/v1/chat/completions", json=body).text
    frames = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
    calls = [tc for f in frames for ch in f["choices"] for tc in ch["delta"].get("tool_calls", [])]
    assert len(calls) == 2
    assert all(isinstance(tc["id"], str) and tc["id"].startswith("call_") for tc in calls)
    assert calls[0]["id"] == calls[1]["id"] and all(tc["type"] == "function" for tc in calls)
    assert frames[0]["choices"][0]["delta"]["content"] == "I'll look." and text.rstrip().endswith(
        "data: [DONE]"
    )
    assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"  # a call with a name was sent


def stream_of(
    *deltas: dict[str, Any], finish: str = "tool_calls"
) -> Callable[[httpx.Request], httpx.Response]:
    """An upstream that streams one choice: `deltas`, then a last frame that ends it with `finish`."""

    def answer(request: httpx.Request) -> httpx.Response:
        frames = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
        frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    return answer


def finish_reasons(
    tmp_path: Path, upstream: Callable[[httpx.Request], httpx.Response], stream: bool
) -> list[str]:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(upstream))
    body = {"model": "base", "stream": stream, "messages": [{"role": "user", "content": "find html"}]}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json=body)
    if not stream:
        return [ch["finish_reason"] for ch in r.json()["choices"]]
    frames = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
    return [ch["finish_reason"] for f in frames for ch in f["choices"] if ch["finish_reason"]]


@pytest.mark.parametrize(
    "deltas",
    [
        (),  # the parser dropped the malformed call: nothing was sent
        ({"content": "<function=glob>"},),  # the call's text came through as text
        ({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},),  # arguments, but no name
    ],
    ids=["no call", "text only", "no name"],
)
def test_a_stream_that_ends_for_tool_calls_without_a_named_call_ends_as_stop(
    tmp_path: Path, deltas: tuple[dict[str, Any], ...]
) -> None:
    """OpenCode 2.0 fails the whole turn on a "tool_calls" with no call ("OpenAI Chat tool call delta is
    missing id or name"); a "stop" reaches it as a reply."""
    assert finish_reasons(tmp_path, stream_of(*deltas), stream=True) == ["stop"]
    named = {"tool_calls": [{"index": 0, "function": {"name": "glob", "arguments": "{}"}}]}
    assert finish_reasons(tmp_path, stream_of(*deltas, named), stream=True) == ["tool_calls"]
    assert finish_reasons(tmp_path, stream_of(*deltas, finish="length"), stream=True) == ["length"]


@pytest.mark.parametrize(
    ("calls", "finish"),
    [
        (None, "stop"),
        ([], "stop"),
        (
            [{"type": "function", "function": {"name": "glob", "arguments": "{}"}}],
            "tool_calls",
        ),  # given an id
        ([{"id": "call_1", "type": "function", "function": {"arguments": "{}"}}], "stop"),  # no name
        (
            [{"id": "call_1", "type": "function", "function": {"name": "glob", "arguments": "{}"}}],
            "tool_calls",
        ),
    ],
    ids=["none", "empty", "no id", "no name", "whole"],
)
def test_a_reply_that_ends_for_tool_calls_without_a_whole_call_ends_as_stop(
    tmp_path: Path, calls: list[dict[str, Any]] | None, finish: str
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "",
            **({"tool_calls": calls} if calls is not None else {}),
        }
        reply = {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}]}
        return httpx.Response(200, json=reply)

    assert finish_reasons(tmp_path, answer, stream=False) == [finish]


def test_a_whole_reply_gives_a_call_without_an_id_or_a_type_both_as_a_stream_does(tmp_path: Path) -> None:
    calls = [
        {"function": {"name": "glob", "arguments": "{}"}},
        {"id": "", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        {"id": "call_kept", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
    ]

    def answer(request: httpx.Request) -> httpx.Response:
        message = {"role": "assistant", "content": "", "tool_calls": calls}
        return httpx.Response(
            200, json={"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}]}
        )

    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(answer))
    body = {"model": "base", "messages": [{"role": "user", "content": "find html"}]}
    with TestClient(app) as c:
        [choice] = c.post("/v1/chat/completions", json=body).json()["choices"]
    got = choice["message"]["tool_calls"]
    assert choice["finish_reason"] == "tool_calls"
    assert [c["id"].startswith("call_") and len(c["id"]) == 29 for c in got[:2]] == [True, True]
    assert got[0]["id"] != got[1]["id"] and got[2]["id"] == "call_kept"
    assert [c["type"] for c in got] == ["function"] * 3
