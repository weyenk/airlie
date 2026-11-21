"""task serve: OpenAI-compatible front door. /v1/models lists roles; chat completions proxy to the engine with
the role→served-model mapping; /v1/airlie/run launches a team run."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.obs import RunDir
from airlie.orchestrator import RunReport
from airlie.prompts import text
from airlie.serve.app import create_app
from airlie.teams import TeamResult
from tests.support.serve import FakeUpstream, cfg
from tests.unit.serve_helpers import TestClient


def test_models_lists_roles(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    r = TestClient(app).get("/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert sorted(ids[:4]) == ["aggregator", "architect", "judge", "summarizer"]  # roles first
    assert sorted(ids[4:]) == ["base", "small"]  # then the raw catalog models


def test_chat_completion_maps_role_to_served_model_and_prepends_system_prompt(tmp_path: Path) -> None:
    up = FakeUpstream()
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(up))
    r = TestClient(app).post(
        "/v1/chat/completions", json={"model": "summarizer", "messages": [{"role": "user", "content": "yo"}]}
    )
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hi"
    sent = json.loads(up.requests[0].content)
    assert sent["model"] == "tiny"  # passthru mapping
    assert sent["messages"][0] == {"role": "system", "content": "x"}  # role prompt prepended
    assert str(up.requests[0].url).startswith("http://engine.test/v1/chat/completions")


def test_chat_completion_streams_through(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "architect", "stream": True, "messages": [{"role": "user", "content": "yo"}]},
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
        "/v1/chat/completions", json={"model": "architect", "messages": [{"role": "user", "content": "yo"}]}
    )
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "length" and choice["message"]["content"] == ""
    assert choice["message"]["reasoning_content"] == "Let me think"  # what the engine sent is passed on
    assert (
        json.loads(up.requests[0].content)["max_tokens"] == c.roles["architect"].max_tokens
    )  # the role's own


def test_a_streamed_reply_cut_off_at_max_tokens_ends_with_finish_reason_length(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(CutOffUpstream()))
    with TestClient(app).stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "architect", "stream": True, "messages": [{"role": "user", "content": "yo"}]},
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes())
    assert b'"finish_reason":"length"' in body and b"[DONE]" in body


ASK = {"messages": [{"role": "user", "content": "yo"}]}


def _sent(up: FakeUpstream) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in up.requests]


def test_an_openai_role_is_sent_its_thinking_default_and_template_kwargs(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.roles["judge"] = c.roles["judge"].model_copy(
        update={"thinking": False, "chat_template_kwargs": {"reasoning_strength": "low"}}
    )
    up = FakeUpstream()
    client = TestClient(create_app(c, transport=httpx.MockTransport(up)))
    client.post("/v1/chat/completions", json={"model": "architect", **ASK})
    client.post("/v1/chat/completions", json={"model": "judge", **ASK})
    architect, judge = _sent(up)
    assert architect["chat_template_kwargs"] == {"enable_thinking": c.roles["architect"].thinking}
    assert judge["chat_template_kwargs"] == {"enable_thinking": False, "reasoning_strength": "low"}
    for body in (architect, judge):  # the openai wire has no top-level thinking fields
        assert not {"enable_thinking", "thinking_budget", "tool_parser", "thinking"} & set(body)


def test_a_clients_template_kwargs_win_over_the_roles_default_and_the_rest_merge(tmp_path: Path) -> None:
    up = FakeUpstream()
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(up)))
    client.post(
        "/v1/chat/completions",
        json={"model": "architect", "chat_template_kwargs": {"enable_thinking": False, "x": 1}, **ASK},
    )
    client.post("/v1/chat/completions", json={"model": "architect", "chat_template_kwargs": {"x": 2}, **ASK})
    first, second = _sent(up)
    assert first["chat_template_kwargs"] == {"enable_thinking": False, "x": 1}
    assert second["chat_template_kwargs"] == {"enable_thinking": True, "x": 2}


def test_a_raw_catalog_model_gets_no_thinking_default(tmp_path: Path) -> None:
    up = FakeUpstream()
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(up)))
    client.post("/v1/chat/completions", json={"model": "small", **ASK})
    sent = _sent(up)[0]
    assert sent["model"] == "tiny" and "chat_template_kwargs" not in sent


def test_the_proxy_leaves_reasoning_effort_and_tool_turns_as_the_client_sent_them(tmp_path: Path) -> None:
    up = FakeUpstream()
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(up)))
    tool_turn = [
        {"role": "user", "content": "run"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
    ]
    client.post(
        "/v1/chat/completions",
        json={"model": "architect", "reasoning_effort": "high", "messages": tool_turn},
    )
    sent = _sent(up)[0]
    assert sent["reasoning_effort"] == "high"  # no remap: the server reads what the client sent
    assert sent["messages"][1:] == tool_turn  # no "Continue." turn appended


def anthropic_cfg(tmp_path: Path) -> Any:
    c = cfg(tmp_path)
    c.engines["main"] = c.engines["main"].model_copy(
        update={"format": "anthropic", "base_url": "https://api.anthropic.test/v1", "api_key": "sk-ant"}
    )
    c.roles["architect"] = c.roles["architect"].model_copy(
        update={"thinking": True, "thinking_budget": 2000, "max_tokens": 4000}
    )
    c.roles["judge"] = c.roles["judge"].model_copy(update={"thinking": False})
    return c


class FakeAnthropic:
    """An Anthropic Messages server: one canned text reply, whole or streamed."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if self.status != 200:
            error = {
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid x-api-key"},
            }
            return httpx.Response(self.status, json=error)
        if body.get("stream"):
            events = [
                ("message_start", {"message": {"id": "m1", "model": "small", "usage": {"input_tokens": 4}}}),
                ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hi"}}),
                ("content_block_stop", {"index": 0}),
                ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
                ("message_stop", {}),
            ]
            sse = "".join(f"event: {k}\ndata: {json.dumps({'type': k, **v})}\n\n" for k, v in events)
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )


def test_an_anthropic_role_is_posted_to_messages_and_answered_in_chat_completions(tmp_path: Path) -> None:
    up = FakeAnthropic()
    client = TestClient(create_app(anthropic_cfg(tmp_path), transport=httpx.MockTransport(up)))
    r = client.post("/v1/chat/completions", json={"model": "architect", **ASK})
    assert r.status_code == 200
    reply = r.json()
    assert (
        reply["choices"][0]["message"]["content"] == "hi" and reply["choices"][0]["finish_reason"] == "stop"
    )
    assert reply["usage"]["prompt_tokens"] == 4 and reply["usage"]["completion_tokens"] == 2
    request = up.requests[0]
    assert str(request.url) == "https://api.anthropic.test/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant" and request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    sent = json.loads(request.content)
    assert sent["model"] == "base" and sent["system"] == "x"  # the role prompt, as Anthropic's system
    assert sent["messages"] == [{"role": "user", "content": "yo"}]
    assert sent["max_tokens"] == 4000
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 2000}  # the role's thinking
    assert not {"chat_template_kwargs", "temperature", "top_p"} & set(sent)


def test_an_anthropic_role_that_does_not_think_sends_no_thinking_block(tmp_path: Path) -> None:
    up = FakeAnthropic()
    client = TestClient(create_app(anthropic_cfg(tmp_path), transport=httpx.MockTransport(up)))
    client.post("/v1/chat/completions", json={"model": "judge", "temperature": 0.2, "top_p": 0.9, **ASK})
    sent = json.loads(up.requests[0].content)
    assert "thinking" not in sent
    assert sent["temperature"] == 0.2 and "top_p" not in sent  # one of the two


def test_an_anthropic_reply_streams_back_as_chat_completion_chunks(tmp_path: Path) -> None:
    up = FakeAnthropic()
    client = TestClient(create_app(anthropic_cfg(tmp_path), transport=httpx.MockTransport(up)))
    with client.stream(
        "POST", "/v1/chat/completions", json={"model": "architect", "stream": True, **ASK}
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes())
    assert json.loads(up.requests[0].content)["stream"] is True
    assert b'"content":"hi"' in body or b'"content": "hi"' in body
    assert b'"finish_reason":"stop"' in body or b'"finish_reason": "stop"' in body
    assert body.rstrip().endswith(b"data: [DONE]")


def test_an_anthropic_error_status_reaches_the_client(tmp_path: Path) -> None:
    client = TestClient(
        create_app(anthropic_cfg(tmp_path), transport=httpx.MockTransport(FakeAnthropic(status=401)))
    )
    r = client.post("/v1/chat/completions", json={"model": "architect", **ASK})
    assert r.status_code == 401 and "invalid x-api-key" in r.json()["detail"]


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
    why = "the reply was cut off at max_tokens (4096) before it ended; raise roles.architect.max_tokens"

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
    cli_mod._orchestrator = lambda cfg, run_id=None, manager=None: captured.append(cfg)  # type: ignore[assignment]
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
    cli_mod._orchestrator = lambda cfg, run_id=None, manager=None: captured.append(cfg)  # type: ignore[assignment]
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


def test_health_reports_the_serve_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from airlie.serve import app as serve_app

    monkeypatch.setattr(serve_app, "build_commit", lambda: "abc1234")
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    monkeypatch.setattr(serve_app, "build_commit", lambda: "def5678")  # a pull after the start
    with TestClient(app) as c:
        body = c.get("/health").json()
    assert body["commit"] == "abc1234" and body["status"] == "ok"


def test_api_key_guards_the_v1_routes_but_not_health(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="s3cret")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/v1/models").status_code == 401
        assert c.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/v1/models", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        body = {"model": "architect", "messages": [{"role": "user", "content": "hi"}]}
        assert c.post("/v1/chat/completions", json=body).status_code == 401
        ok = c.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


def test_raw_catalog_models_are_served_without_a_prompt(tmp_path: Path) -> None:
    up = FakeUpstream()
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(up))
    with TestClient(app) as c:
        ids = {m["id"] for m in c.get("/v1/models").json()["data"]}
        assert {"architect", "small", "base"} <= ids  # roles and raw catalog models
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
    (tmp_path / "architect.md").write_text("---\nsummary: Implement code\n---\n# architect\nUse write_file.")
    body = {
        "model": "architect",
        "messages": [
            {"role": "system", "content": "You are OpenCode. Tools: write, read, bash."},
            {"role": "user", "content": "hi"},
        ],
    }
    with TestClient(app) as c:
        assert c.post("/v1/chat/completions", json=body).status_code == 200
    sent = json.loads(up.requests[-1].content)["messages"]
    assert [m["role"] for m in sent] == ["system", "system", "user"]
    assert sent[1]["content"].startswith("You are OpenCode")
    # the role prompt, a blank line, the client-bridge rule: the rule's file is one sentence with no blank
    # lines around it, so the separator is the server's
    assert not text("client-bridge").startswith("\n")
    assert sent[0]["content"] == "# architect\nUse write_file.\n\n" + text("client-bridge")


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


def test_a_truncated_upstream_stream_logs_an_abnormal_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(answer))
    body = {"model": "base", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    with caplog.at_level(logging.INFO, logger="airlie.serve.app"), TestClient(app) as c:
        assert c.post("/v1/chat/completions", json=body).status_code == 200
    assert any(
        "chat_completions_stream_end" in r.message and "end_reason=upstream_eof" in r.message
        for r in caplog.records
    )


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


# ---- the line's ticker (planning-line P5b, D3, 2026-10-25) ----


def test_health_reports_the_line(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    with TestClient(app) as c:
        line = c.get("/health").json()["line"]
        assert line == {
            "last_tick": None,
            "line": None,
            "paused": False,
            "lanes": {"build": None, "plan": None},
        }
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state" / "line.paused").write_text("1")
        assert c.get("/health").json()["line"]["paused"] is True


def wait_until(check: Callable[[], bool], seconds: float = 10.0) -> bool:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_serve_ticks_on_its_timer_and_registers_the_lane_jobs(tmp_path: Path) -> None:
    from airlie.story.line import Job

    ran: list[str] = []

    async def beat(spawn: Callable[[Job], None]) -> str:
        async def work() -> str:
            ran.append("AIR-9")
            return "AIR-9 Avatars: review"

        events = tmp_path / "state" / "stories" / "AIR-9" / "events.jsonl"
        spawn(Job("build", "AIR-9", "story", events, work))
        return "started build story AIR-9"

    app = create_app(
        cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), tick_s=0.05, line_beat=beat
    )
    with TestClient(app) as client:
        assert client.get("/health").json()["line"]["last_tick"] is None  # nothing before the first interval
        assert wait_until(lambda: client.get("/health").json()["line"]["last_tick"] is not None)
        assert client.get("/health").json()["line"]["line"] == "started build story AIR-9"
        assert wait_until(lambda: bool(ran))
        [job] = [j for j in client.get("/v1/airlie/jobs").json()["jobs"] if j["id"] == "line-build-AIR-9"]
        assert job["kind"] == "line"
        done = client.get("/v1/airlie/jobs/line-build-AIR-9", params={"wait": 5}).json()
        assert done["kind"] == "line" and done["result"] == {"line": "AIR-9 Avatars: review"}


async def test_every_beat_uses_one_board_and_a_changed_board_replaces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-04: each beat made its own board, and so its own SQLite connection to board.sqlite, and closed
    none; a beat's job can outlive it, so nothing closed it but the cycle collector, which an idle serve
    rarely runs. Over a night serve reached launchd's 256 open files and every /health and tick failed with
    EMFILE. One board now serves every beat; a change to the board's config replaces it, the old one
    closed."""
    from airlie.serve.jobs import Jobs
    from airlie.serve.ticker import LineTicker
    from airlie.story import factory
    from airlie.story.line import Line

    class Board:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    made: list[Board] = []
    used: list[object] = []

    def make_board(scfg: Any) -> Board:
        made.append(Board())
        return made[-1]

    async def beat(self: Line, spawn: Any) -> str:
        used.append(self.board)
        return "idle"

    c = cfg(tmp_path)
    monkeypatch.setattr(factory, "story_config", lambda cfg: cfg)
    monkeypatch.setattr(factory, "story_problems", lambda cfg: [])
    monkeypatch.setattr(factory, "make_board", make_board)
    monkeypatch.setattr(Line, "beat", beat)
    ticker = LineTicker(c, Jobs(), commit=None)
    for _ in range(3):
        assert await ticker.beat() == "idle"
    assert len(made) == 1 and used == [made[0]] * 3
    ticker.cfg = c.model_copy(update={"board": c.board.model_copy(update={"prefix": "HDS"})})
    await ticker.beat()
    assert len(made) == 2 and made[0].closed and not made[1].closed and used[-1] is made[1]


def test_a_serve_with_ticks_off_never_ticks(tmp_path: Path) -> None:
    import time

    beats: list[str] = []

    async def beat(spawn: Callable[..., None]) -> str:
        beats.append("beat")
        return "idle"

    c = cfg(tmp_path).model_copy(deep=True)
    c.line.serve_ticks = False
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()), tick_s=0.02, line_beat=beat)
    with TestClient(app):
        time.sleep(0.3)
    assert beats == []


def test_the_line_row_shows_both_lanes_the_gates_and_the_blocked_cards(tmp_path: Path) -> None:
    """Planning-line P5b, D11: one row for the Vue app, true whichever process ticked."""
    import json
    import time

    from airlie.board import BoardStore
    from airlie.story.diagnosis import Classified, Diagnosis, Pack, Paths, write_diagnosis
    from airlie.story.phase import write_phase
    from airlie.story.state import lane_lock

    c = cfg(tmp_path)
    state = c.observability.state_dir
    store = BoardStore(state / "board.sqlite")
    blocked = store.create(level="story", title="Stuck", labels=["repo:o/r", "blocked_by:AIR-9"]).key
    store.update(blocked, status="blocked")
    building = store.create(level="story", title="Avatars").key
    pack = Pack(
        key=blocked, n=1, reason="x", stage="layers", layer="01", attempt=1, run_id=None, subtask=None,
        role=None, stop_reason=None, refused_calls=0, holds=(), repeats=0, continuations=0, steps=0,
        tokens=0, minutes=0.0, gate_tail="", tracebacks=(), failures=(), base_gate_failed=None,
        base_failing=(), diff_stat="", paths=Paths(state / "e", None, None, ()),
    )  # fmt: skip
    write_diagnosis(
        state,
        Diagnosis(
            pack,
            Classified("config", "cut_off", "layers", "config:cut_off:architect", ("x",), "r"),
            None,
            "n",
        ),
    )
    story = state / "stories" / building
    story.mkdir(parents=True)
    (story / "state.json").write_text(json.dumps({"stage": "layers", "card": {"key": building}}))
    (story / "events.jsonl").write_text(json.dumps({"kind": "layer_start", "layer": "02"}) + "\n")
    run = state / "runs" / "R1"
    run.mkdir(parents=True)
    steps = [{"kind": "step", "step": n, "completion_tokens": 100, "gen_ms": 4000.0} for n in range(1, 6)]
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in steps))
    write_phase(state / "stories", "decoding", run_dir=run)
    (state / "line" / "lanes").mkdir(parents=True)
    (state / "line" / "lanes" / "build.json").write_text(
        json.dumps({"key": building, "what": "story", "started": time.time() - 90})
    )
    (state / "line" / "status.json").write_text(
        json.dumps(
            {"ts": 1700000000.0, "gates": {"engine": "ok", "profile_match": "mismatch", "github": "ok"}}
        )
    )
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    auth = {"Authorization": "Bearer k"}
    with TestClient(app) as client:
        free = client.get("/v1/airlie/line", headers=auth).json()
        assert free["lanes"] == {
            "build": None,
            "plan": None,
        }  # nothing holds a lane: a leftover file is not believed
        with lane_lock(
            state / "stories", "build"
        ) as held:  # the build lane is held, as a running job holds it
            assert held
            row = client.get("/v1/airlie/line", headers=auth).json()
        assert row["paused"] is False and row["last_tick"] == 1700000000.0
        assert row["gates"] == {"engine": "ok", "profile_match": "mismatch", "github": "ok"}
        build = row["lanes"]["build"]
        assert (build["card"], build["title"], build["stage"], build["layer"]) == (
            building,
            "Avatars",
            "layers",
            "02",
        )
        assert (build["step"], build["tps"]) == (5, 25.0) and 85 < build["elapsed_s"] < 200
        assert row["lanes"]["plan"] is None and row["queue_hours"] == 0.0
        assert row["blocked"] == [{"key": blocked, "class": "config", "kind": "cut_off", "fix": "AIR-9"}]
        assert client.get("/v1/airlie/line").status_code == 401


def test_the_line_events_tail(tmp_path: Path) -> None:
    import json

    c = cfg(tmp_path)
    log = c.observability.state_dir / "line" / "events.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("".join(json.dumps({"kind": "tick", "line": f"beat {i}"}) + "\n" for i in range(10)))
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    auth = {"Authorization": "Bearer k"}
    with TestClient(app) as client:
        got = client.get("/v1/airlie/line/events", params={"limit": 3}, headers=auth).json()["events"]
        assert [e["line"] for e in got] == ["beat 7", "beat 8", "beat 9"]  # the tail, oldest first
        assert len(client.get("/v1/airlie/line/events", headers=auth).json()["events"]) == 10
        assert client.get("/v1/airlie/line/events", params={"limit": 0}, headers=auth).status_code == 422


def test_an_openai_http_role_gets_a_clients_template_kwargs_unchanged(tmp_path: Path) -> None:
    up = FakeUpstream()
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(up)))
    ask = {"messages": [{"role": "user", "content": "yo"}]}
    kwargs = {"enable_thinking": False, "x": 1}
    client.post("/v1/chat/completions", json={"model": "judge", "chat_template_kwargs": kwargs, **ask})
    client.post("/v1/chat/completions", json={"model": "judge", "thinking_token_budget": 9, **ask})
    first, second = (json.loads(r.content) for r in up.requests)
    assert first["chat_template_kwargs"] == kwargs and "enable_thinking" not in first
    assert second["thinking_token_budget"] == 9 and "thinking_budget" not in second
    assert "tool_parser" not in first
