"""Bridge platform tests T10–T12: studio kind mapping, serve 404 → failed, claim loop with fake clock."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from airlie.config import load_config
from airlie.config.schema import AllowedRepoSpec, BridgeSpec, LlmKindSpec
from airlie.serve.bridge.airlie_client import ServeClient
from airlie.serve.bridge.clock import FakeClock
from airlie.serve.bridge.errors import BridgeError
from airlie.serve.bridge.job_bus import HubJob, JobBusClient
from airlie.serve.bridge.kinds import LlmKind, StudioKind, map_run_result
from airlie.serve.bridge.payload import assert_data_payload
from airlie.serve.bridge.repos import resolve_repo
from airlie.serve.bridge.runner import BridgeRunner, run_bridge

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml


def _cfg_with_bridge(bridge: BridgeSpec) -> Any:
    cfg = load_config(ROOT / "airlie.yaml", local_path=NO_LOCAL)
    cfg.bridge = bridge
    cfg.engines = {}  # the busy probe must not look at this machine's engines or story locks
    cfg.observability.state_dir = Path(tempfile.mkdtemp(prefix="bridge-state-"))
    return cfg


def bridge_cfg(tmp_path: Path, **over: Any) -> BridgeSpec:
    repo = tmp_path / "proj"
    repo.mkdir()
    return BridgeSpec(
        job_bus_url="https://hub.test/api/v1",
        allowed_repos=[AllowedRepoSpec(key="main", path=repo.resolve(), default=True)],
        test_cmds=["pytest -q"],
        **over,
    )


class FakeHub(httpx.AsyncBaseTransport):
    def __init__(self, *, jobs: list[dict[str, Any]] | None = None) -> None:
        self.claim_calls = 0
        self.results: list[dict[str, Any]] = []
        self.heartbeats: list[str] = []
        self.heartbeat_status = 200
        self.heartbeat_raises: Exception | None = None
        self._queue = list(jobs or [])

    def enqueue(self, job: dict[str, Any]) -> None:
        self._queue.append(job)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/jobs/claim"):
            self.claim_calls += 1
            if not self._queue:
                return httpx.Response(204)
            job = self._queue.pop(0)
            return httpx.Response(200, json={"job": job})
        if request.method == "POST" and request.url.path.endswith("/result"):
            self.results.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and request.url.path.endswith("/heartbeat"):
            self.heartbeats.append(request.url.path.split("/")[-2])
            if self.heartbeat_raises is not None:
                raise self.heartbeat_raises
            return httpx.Response(self.heartbeat_status, json={"ok": self.heartbeat_status == 200})
        return httpx.Response(404, json={"error": "unexpected hub route"})


class FakeServe(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.chat_posts: list[dict[str, Any]] = []
        self.chat_read_timeouts: list[float | None] = []
        self._state = "running"
        self._result: dict[str, Any] = {}
        self._job_id = "20261028-test-run"
        self.polls = 0
        self._missing = False
        self._chat_content: str | None = "hello from serve"
        self._chat_status = 200
        self._chat_body: dict[str, Any] | None = None

    def set_done(self, result: dict[str, Any]) -> None:
        self._result = result
        self._state = "done"

    def set_missing(self) -> None:
        self._missing = True

    def set_chat_content(self, content: str | None) -> None:
        self._chat_content = content
        self._chat_body = None

    def set_chat_response(self, status: int, body: dict[str, Any]) -> None:
        self._chat_status = status
        self._chat_body = body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/airlie/runs":
            self.posts.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": self._job_id, "kind": "run"})
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            self.chat_posts.append(json.loads(request.content))
            self.chat_read_timeouts.append(request.extensions.get("timeout", {}).get("read"))
            if self._chat_body is not None:
                return httpx.Response(self._chat_status, json=self._chat_body)
            if self._chat_content is None:
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"role": "assistant", "content": ""}}]},
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": self._chat_content}}]},
            )
        if request.method == "GET" and request.url.path.startswith("/v1/airlie/jobs/"):
            self.polls += 1
            if self._missing:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(
                200,
                json={"state": self._state, "result": self._result, "next": 0, "events": []},
            )
        return httpx.Response(404)


def _llm_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "prompt": "summarise the week",
        "persona": "# Writer\n\nYou write concise summaries.",
        "output": "chat.completions",
        "caller": "lhr",
        "model": None,
        "thinking": None,
    }
    base.update(over)
    return base


ENV = {"BRIDGE_WORKER_SECRET": "s" * 40}


def test_credential_env_reads_bridge_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker secrets come from ~/.config/airlie/bridge.env, not the shared ATLASSIAN .env."""
    home = tmp_path / "home"
    airlie_cfg = home / ".config" / "airlie"
    airlie_cfg.mkdir(parents=True)
    dotenv = airlie_cfg / "bridge.env"
    dotenv.write_text("BRIDGE_WORKER_SECRET=" + "b" * 40 + "\n")
    dotenv.chmod(0o600)
    (airlie_cfg / ".env").write_text("BRIDGE_WORKER_SECRET=" + "wrong" * 8 + "\n")
    monkeypatch.setenv("HOME", str(home))
    for key in ("BRIDGE_WORKER_SECRET", "AIRLIE_SERVE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    cfg = _cfg_with_bridge(bridge_cfg(tmp_path))
    from airlie.serve.bridge.session import credential_env, worker_secret

    assert worker_secret(cfg.bridge, credential_env(cfg)) == "b" * 40


def test_resolve_serve_api_key_reads_bridge_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """task serve and task bridge share AIRLIE_SERVE_API_KEY from bridge.env — no --api-key every time."""
    home = tmp_path / "home"
    airlie_cfg = home / ".config" / "airlie"
    airlie_cfg.mkdir(parents=True)
    dotenv = airlie_cfg / "bridge.env"
    dotenv.write_text("AIRLIE_SERVE_API_KEY=serve-secret-from-file\n")
    dotenv.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AIRLIE_SERVE_API_KEY", raising=False)
    cfg = _cfg_with_bridge(bridge_cfg(tmp_path))
    from airlie.serve.bridge.session import resolve_serve_api_key

    assert resolve_serve_api_key(cfg) == "serve-secret-from-file"
    assert resolve_serve_api_key(cfg, explicit="cli-wins") == "cli-wins"


def test_bridge_dotenv_wins_over_stale_shell_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong AIRLIE_SERVE_API_KEY left in the shell must not override bridge.env."""
    home = tmp_path / "home"
    airlie_cfg = home / ".config" / "airlie"
    airlie_cfg.mkdir(parents=True)
    dotenv = airlie_cfg / "bridge.env"
    dotenv.write_text("AIRLIE_SERVE_API_KEY=from-file\nBRIDGE_WORKER_SECRET=" + "b" * 40 + "\n")
    dotenv.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AIRLIE_SERVE_API_KEY", "stale-from-shell")
    cfg = _cfg_with_bridge(bridge_cfg(tmp_path))
    from airlie.serve.bridge.session import credential_env, resolve_serve_api_key, serve_api_key

    assert serve_api_key(cfg.bridge, credential_env(cfg)) == "from-file"
    # No --api-key: file wins over stale shell. CLI --api-key still wins via explicit=.
    assert resolve_serve_api_key(cfg) == "from-file"
    assert resolve_serve_api_key(cfg, explicit="stale-from-shell") == "stale-from-shell"


# ---- T10: studio mapping, plan=None, report dropped, repo + payload rules ----


def test_t10_studio_posts_plan_none_and_maps_result_without_report(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path)
    serve_t = FakeServe()
    serve_t.set_done(
        {
            "ok": True,
            "final_answer": "done",
            "branch": "airlie/x",
            "tests": {"studio": True},
            "report": "/tmp/report.md",
        }
    )
    client = ServeClient("http://127.0.0.1:8090", transport=serve_t)
    kind = StudioKind(bridge, client, clock=FakeClock())
    out = asyncio.run(kind.run({"goal": "fix it", "repo_key": "main"}))
    assert out.status == "done" and out.result == {
        "ok": True,
        "final_answer": "done",
        "branch": "airlie/x",
        "tests": {"studio": True},
    }
    assert "report" not in (out.result or {})
    assert serve_t.posts == [
        {
            "goal": "fix it",
            "repo": str(bridge.allowed_repos[0].path),
            "plan": None,
            "test_cmd": "pytest -q",
        }
    ]


def test_t10_rejects_control_fields_and_resolves_default_repo(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path)
    with pytest.raises(BridgeError, match="control fields"):
        assert_data_payload({"goal": "g", "repo": "/evil"})
    assert resolve_repo(bridge, None) == bridge.allowed_repos[0].path
    with pytest.raises(BridgeError, match="unknown repo_key"):
        resolve_repo(bridge, "nope")


def test_t10_map_run_answer_type() -> None:
    assert map_run_result({"type": "answer", "answer": "42", "failed": False}) == {
        "ok": True,
        "final_answer": "42",
    }


# ---- T11: serve job 404 → hub failed ----


def test_t11_serve_404_yields_failed_outcome(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path)
    serve_t = FakeServe()
    serve_t.set_missing()
    client = ServeClient("http://127.0.0.1:8090", transport=serve_t)
    out = asyncio.run(StudioKind(bridge, client).run({"goal": "x"}))
    assert out.status == "failed"
    assert out.error_message and "404" in out.error_message


# ---- T12: runner claim loop with fake clock ----


def test_t12_empty_queue_waits_claim_poll_on_fake_clock(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path, claim_poll_s=5.0)
    cfg = _cfg_with_bridge(bridge)
    hub_t = FakeHub()
    clock = FakeClock()

    n = asyncio.run(
        run_bridge(
            cfg,
            env=ENV,
            hub_transport=hub_t,
            serve_transport=FakeServe(),
            clock=clock,
            max_cycles=1,
        )
    )
    assert n == 0
    assert hub_t.claim_calls == 1
    assert clock.sleeps == [5.0]


def test_t12_runner_claims_one_job_and_posts_hub_result(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path)
    cfg = _cfg_with_bridge(bridge)
    jid = str(uuid.uuid4())
    hub_t = FakeHub(
        jobs=[
            {
                "id": jid,
                "kind": "studio",
                "tenant_id": "lhr",
                "payload": {"goal": "ship"},
                "worker_id": "w1",
                "status": "running",
            }
        ]
    )
    serve_t = FakeServe()
    serve_t.set_done({"ok": True, "final_answer": "y", "branch": "b", "tests": {}})

    n = asyncio.run(
        run_bridge(
            cfg,
            env=ENV,
            once=True,
            hub_transport=hub_t,
            serve_transport=serve_t,
        )
    )
    assert n == 1 and hub_t.claim_calls == 1
    assert hub_t.results[0]["status"] == "done"
    assert hub_t.results[0]["result"] == {"ok": True, "final_answer": "y", "branch": "b", "tests": {}}


# ---- llm kind: payload validation + chat completions ----


def test_llm_happy_path_posts_chat_completions(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path, kinds=["studio", "llm"])
    serve_t = FakeServe()
    serve_t.set_chat_content("weekly digest")
    client = ServeClient("http://127.0.0.1:8090", transport=serve_t)
    out = asyncio.run(LlmKind(bridge, client).run(_llm_payload()))
    assert out.status == "done"
    assert out.result == {"output": "chat.completions", "content": "weekly digest"}
    assert serve_t.chat_posts == [
        {
            "model": "generalist",
            "messages": [
                {"role": "system", "content": "# Writer\n\nYou write concise summaries."},
                {"role": "user", "content": "summarise the week"},
            ],
            "max_tokens": 16384,
            "chat_template_kwargs": {"enable_thinking": True},
            "thinking_token_budget": 8192,
        }
    ]


def test_llm_uses_bridge_llm_settings(tmp_path: Path) -> None:
    llm = LlmKindSpec(model="small", max_tokens=4096, thinking=False, thinking_budget=None)
    bridge = bridge_cfg(tmp_path, kinds=["llm"], llm=llm)
    serve_t = FakeServe()
    client = ServeClient("http://127.0.0.1:8090", transport=serve_t)
    out = asyncio.run(LlmKind(bridge, client).run(_llm_payload()))
    assert out.status == "done"
    body = serve_t.chat_posts[0]
    assert body["model"] == "small"
    assert body["max_tokens"] == 4096
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "thinking_token_budget" not in body


def test_llm_spec_rejects_max_tokens_under_budget() -> None:
    with pytest.raises(ValidationError):
        LlmKindSpec(max_tokens=100, thinking_budget=200)


def test_llm_chat_timeout_is_the_spec_timeout(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path, kinds=["llm"])
    serve_t = FakeServe()
    client = ServeClient("http://127.0.0.1:8090", transport=serve_t)
    asyncio.run(LlmKind(bridge, client).run(_llm_payload()))
    assert serve_t.chat_read_timeouts == [900.0]


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"prompt": ""}, "non-empty prompt"),
        ({"prompt": "   "}, "non-empty prompt"),
        ({"persona": ""}, "non-empty persona"),
        ({"output": "embeddings"}, "chat.completions"),
        ({"caller": "other"}, "caller"),
        ({"model": "gpt-4"}, "model is not supported yet"),
        ({"thinking": True}, "thinking is not supported yet"),
    ],
)
def test_llm_rejects_invalid_payload(tmp_path: Path, over: dict[str, Any], match: str) -> None:
    bridge = bridge_cfg(tmp_path, kinds=["llm"])
    serve_t = FakeServe()
    client = ServeClient("http://127.0.0.1:8090", transport=serve_t)
    out = asyncio.run(LlmKind(bridge, client).run(_llm_payload(**over)))
    assert out.status == "failed"
    assert out.error_message and match in out.error_message
    assert serve_t.chat_posts == []


def test_llm_rejects_empty_assistant_content(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path, kinds=["llm"])
    serve_t = FakeServe()
    serve_t.set_chat_content(None)
    client = ServeClient("http://127.0.0.1:8090", transport=serve_t)
    out = asyncio.run(LlmKind(bridge, client).run(_llm_payload()))
    assert out.status == "failed"
    assert out.error_message and "no assistant content" in out.error_message
    assert len(serve_t.chat_posts) == 1


def test_llm_runner_claims_and_posts_hub_result(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path, kinds=["studio", "llm"])
    cfg = _cfg_with_bridge(bridge)
    jid = str(uuid.uuid4())
    hub_t = FakeHub(
        jobs=[
            {
                "id": jid,
                "kind": "llm",
                "tenant_id": "lhr",
                "payload": _llm_payload(),
                "worker_id": "w1",
                "status": "running",
            }
        ]
    )
    serve_t = FakeServe()
    serve_t.set_chat_content("ok")
    n = asyncio.run(
        run_bridge(
            cfg,
            env=ENV,
            once=True,
            hub_transport=hub_t,
            serve_transport=serve_t,
        )
    )
    assert n == 1
    assert hub_t.results[0]["status"] == "done"
    assert hub_t.results[0]["result"] == {"output": "chat.completions", "content": "ok"}


# ---- busy gate: claim only while airlie is idle ----


def _runner(tmp_path: Path, busy: Any, hub_t: FakeHub) -> BridgeRunner:
    bridge = bridge_cfg(tmp_path)
    return BridgeRunner(
        cfg=_cfg_with_bridge(bridge),
        hub=JobBusClient(bridge.job_bus_url or "", "s" * 40, transport=hub_t),
        serve=ServeClient(bridge.serve_url, api_key="k", transport=FakeServe()),
        worker="w1",
        clock=FakeClock(),
        bridge=bridge,
        stop=asyncio.Event(),
        busy=busy,
    )


def test_busy_bridge_does_not_claim(tmp_path: Path) -> None:
    async def busy() -> str | None:
        return "story build lane is running"

    hub_t = FakeHub()
    assert asyncio.run(_runner(tmp_path, busy, hub_t).tick()) is False
    assert hub_t.claim_calls == 0


def test_idle_bridge_claims(tmp_path: Path) -> None:
    async def busy() -> str | None:
        return None

    hub_t = FakeHub()
    asyncio.run(_runner(tmp_path, busy, hub_t).tick())
    assert hub_t.claim_calls == 1


def test_busy_reason_logged_once_per_change(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    reasons = ["story build lane is running", "story build lane is running", "story plan lane is running"]

    async def busy() -> str | None:
        return reasons.pop(0)

    runner = _runner(tmp_path, busy, FakeHub())

    async def go() -> None:
        for _ in range(3):
            await runner.tick()

    with caplog.at_level(logging.INFO, logger="airlie.serve.bridge.runner"):
        asyncio.run(go())
    assert sum("story build lane is running" in r.getMessage() for r in caplog.records) == 1
    assert sum("story plan lane is running" in r.getMessage() for r in caplog.records) == 1


# ---- heartbeat: keep a running hub job leased ----


class _WaitingHandler:
    """Runs until the hub has seen `beats` heartbeats (the fake clock drives them)."""

    def __init__(self, hub_t: FakeHub, beats: int) -> None:
        self.hub_t = hub_t
        self.beats = beats

    async def run(self, payload: dict[str, Any]) -> Any:
        from airlie.serve.bridge.kinds import KindOutcome

        while len(self.hub_t.heartbeats) < self.beats:
            await asyncio.sleep(0)
        return KindOutcome("done", result={"ok": True})


def _heartbeat_runner(
    tmp_path: Path, hub_t: FakeHub, beats: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[BridgeRunner, str]:
    runner = _runner(tmp_path, None, hub_t)
    runner.bridge = runner.bridge.model_copy(update={"heartbeat_s": 60.0})
    monkeypatch.setattr(
        "airlie.serve.bridge.runner.handler_for", lambda *a, **k: _WaitingHandler(hub_t, beats)
    )
    return runner, str(uuid.uuid4())


def _job(jid: str) -> HubJob:
    return HubJob(id=jid, kind="llm", tenant_id="lhr", payload={}, worker_id="w1")


def test_running_job_sends_heartbeats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub_t = FakeHub()
    runner, jid = _heartbeat_runner(tmp_path, hub_t, 2, monkeypatch)
    asyncio.run(runner.process_job(_job(jid)))
    assert hub_t.heartbeats == [jid, jid]
    assert hub_t.results[0]["status"] == "done"
    assert runner.clock.sleeps[:2] == [60.0, 60.0]  # type: ignore[attr-defined]


def test_heartbeat_failure_does_not_stop_the_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub_t = FakeHub()
    hub_t.heartbeat_status = 409
    runner, jid = _heartbeat_runner(tmp_path, hub_t, 2, monkeypatch)
    asyncio.run(runner.process_job(_job(jid)))
    assert len(hub_t.heartbeats) >= 2
    assert hub_t.results[0]["status"] == "done"


def test_heartbeat_stops_when_job_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub_t = FakeHub()
    runner, jid = _heartbeat_runner(tmp_path, hub_t, 1, monkeypatch)

    async def go() -> int:
        await runner.process_job(_job(jid))
        seen = len(hub_t.heartbeats)
        for _ in range(5):
            await runner.clock.sleep(600)
            for _ in range(5):
                await asyncio.sleep(0)
        return seen

    seen = asyncio.run(go())
    assert len(hub_t.heartbeats) == seen


def test_unexpected_heartbeat_error_does_not_fail_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_t = FakeHub()
    hub_t.heartbeat_raises = ValueError("malformed")
    runner, jid = _heartbeat_runner(tmp_path, hub_t, 2, monkeypatch)
    asyncio.run(runner.process_job(_job(jid)))
    assert hub_t.results[0]["status"] == "done"


# ---- a serve outage or a broken handler fails the job, never the bridge ----


class DownServe(httpx.AsyncBaseTransport):
    """serve is not listening: every request is a ConnectError (with `start_ok`, all but starting a run)."""

    def __init__(self, *, start_ok: bool = False) -> None:
        self.start_ok = start_ok

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.start_ok and request.method == "POST" and request.url.path == "/v1/airlie/runs":
            return httpx.Response(202, json={"job_id": "j1", "kind": "run"})
        raise httpx.ConnectError("connection refused", request=request)


def test_llm_serve_down_fails_the_job(tmp_path: Path) -> None:
    bridge = bridge_cfg(tmp_path, kinds=["llm"])
    client = ServeClient("http://127.0.0.1:8090", transport=DownServe())
    out = asyncio.run(LlmKind(bridge, client).run(_llm_payload()))
    assert out.status == "failed"
    assert out.error_message and out.error_message.startswith("serve unreachable:")


def test_llm_serve_read_timeout_fails_the_job(tmp_path: Path) -> None:
    class SlowServe(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

    bridge = bridge_cfg(tmp_path, kinds=["llm"])
    client = ServeClient("http://127.0.0.1:8090", transport=SlowServe())
    out = asyncio.run(LlmKind(bridge, client).run(_llm_payload()))
    assert out.status == "failed"
    assert out.error_message and "serve unreachable" in out.error_message


@pytest.mark.parametrize("start_ok", [False, True])
def test_studio_serve_down_fails_the_job(tmp_path: Path, start_ok: bool) -> None:
    bridge = bridge_cfg(tmp_path)
    client = ServeClient("http://127.0.0.1:8090", transport=DownServe(start_ok=start_ok))
    out = asyncio.run(StudioKind(bridge, client, clock=FakeClock()).run({"goal": "x"}))
    assert out.status == "failed"
    assert out.error_message and out.error_message.startswith("serve unreachable:")


def test_studio_poll_error_fails_the_job(tmp_path: Path) -> None:
    class BadPoll(FakeServe):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(500, text="boom")
            return await super().handle_async_request(request)

    bridge = bridge_cfg(tmp_path)
    client = ServeClient("http://127.0.0.1:8090", transport=BadPoll())
    out = asyncio.run(StudioKind(bridge, client, clock=FakeClock()).run({"goal": "x"}))
    assert out.status == "failed"
    assert out.error_message and "500" in out.error_message


def _hub_job(jid: str, kind: str = "llm") -> dict[str, Any]:
    return {"id": jid, "kind": kind, "tenant_id": "lhr", "payload": {}, "worker_id": "w1", "status": "running"}


class _RaisingHandler:
    async def run(self, payload: dict[str, Any]) -> Any:
        raise RuntimeError("handler blew up")


def test_a_handler_that_raises_posts_failed_and_the_loop_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    hub_t = FakeHub(jobs=[_hub_job(first), _hub_job(second)])
    runner = _runner(tmp_path, None, hub_t)
    monkeypatch.setattr("airlie.serve.bridge.runner.handler_for", lambda *a, **k: _RaisingHandler())

    async def go() -> list[asyncio.Task[Any]]:
        await runner.run_until_stopped(max_cycles=2)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    left = asyncio.run(go())
    assert left == []  # the heartbeat task was cancelled
    assert [r["status"] for r in hub_t.results] == ["failed", "failed"]
    assert "handler blew up" in hub_t.results[0]["error_message"]
    assert hub_t.claim_calls == 2


def test_a_busy_probe_that_raises_counts_as_busy_and_the_loop_goes_on(tmp_path: Path) -> None:
    calls = 0

    async def busy() -> str | None:
        nonlocal calls
        calls += 1
        raise RuntimeError("probe broke")

    hub_t = FakeHub()
    runner = _runner(tmp_path, busy, hub_t)
    asyncio.run(runner.run_until_stopped(max_cycles=3))
    assert calls == 3
    assert hub_t.claim_calls == 0
