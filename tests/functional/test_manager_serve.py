"""serve owns the manager: chat holds its role's engine (starting a stopped one, a failed start a 503 that
names it), GET /v1/airlie/engines, and the lease routes. Fake engine resources, a clock that only moves when
a test moves it, FakeUpstream for the engine's HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from airlie.manager.core import Manager
from airlie.models_download import DownloadProgress
from airlie.serve.app import create_app
from tests.support.serve import FakeEngine, FakeUpstream, StuckClock, cfg
from tests.unit.serve_helpers import TestClient

ASK = {"model": "coder", "messages": [{"role": "user", "content": "yo"}]}
KEY = {"Authorization": "Bearer test-key"}
ELSEWHERE = ("10.0.0.5", 1)


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine("main")


@pytest.fixture
def clock() -> StuckClock:
    return StuckClock()


@pytest.fixture
def manager(engine: FakeEngine, clock: StuckClock) -> Manager:
    return Manager([engine], budget_gb=100.0, clock=clock)


def serve(tmp_path: Path, manager: Manager, **kw):
    kw.setdefault("transport", httpx.MockTransport(FakeUpstream()))
    return create_app(cfg(tmp_path), manager=manager, **kw)


def status(manager: Manager):
    return next(s for s in manager.status() if s.name == "main")


def test_chat_to_a_stopped_engine_starts_it_and_answers(
    tmp_path: Path, manager: Manager, engine: FakeEngine
) -> None:
    with TestClient(serve(tmp_path, manager)) as c:
        assert status(manager).state == "stopped"
        r = c.post("/v1/chat/completions", json=ASK)
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "hi"
        assert status(manager).state == "ready" and status(manager).holders == ()
    assert engine.up


class SeesHolders(httpx.AsyncByteStream):
    """An SSE body that notes who holds the engine as each chunk is read."""

    def __init__(self, manager: Manager, seen: list[tuple[str, ...]]) -> None:
        self.manager, self.seen = manager, seen

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for frame in (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', b"data: [DONE]\n\n"):
            self.seen.append(status(self.manager).holders)
            yield frame


def test_a_stream_holds_the_engine_until_it_ends(tmp_path: Path, manager: Manager) -> None:
    seen: list[tuple[str, ...]] = []
    sse = {"content-type": "text/event-stream"}
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, stream=SeesHolders(manager, seen), headers=sse)
    )
    with TestClient(serve(tmp_path, manager, transport=transport)) as c:
        r = c.post("/v1/chat/completions", json={**ASK, "stream": True})
        assert r.status_code == 200 and b"[DONE]" in r.content
        assert [len(h) for h in seen] == [1, 1]
        assert seen[0][0].startswith("chat:")
        assert status(manager).holders == ()


def test_a_failed_start_is_a_503_naming_the_engine(
    tmp_path: Path, manager: Manager, engine: FakeEngine
) -> None:
    engine.fail = "port 8081 is taken"
    up = FakeUpstream()
    with TestClient(serve(tmp_path, manager, transport=httpx.MockTransport(up))) as c:
        r = c.post("/v1/chat/completions", json=ASK)
        assert r.status_code == 503
        assert r.json() == {"error": "main failed: port 8081 is taken", "engine": "main"}
        assert up.requests == [] and status(manager).state == "failed"


def test_the_engines_route_names_the_roles_each_engine_serves(tmp_path: Path, manager: Manager) -> None:
    with TestClient(serve(tmp_path, manager)) as c:
        row = c.get("/v1/airlie/engines").json()[0]

    assert row["roles"] == ["aggregator", "architect", "judge", "summarizer"]


def test_the_engines_route_lists_state_and_progress(
    tmp_path: Path, manager: Manager, engine: FakeEngine
) -> None:
    engine.progress = DownloadProgress(done_bytes=5, total_bytes=20, rate_bps=2.0, eta_s=7.5)
    engine.gate = asyncio.Event()
    with TestClient(serve(tmp_path, manager)) as c:
        assert c.get("/v1/airlie/engines").json() == [
            {
                "name": "main",
                "kind": "engine",
                "state": "stopped",
                "detail": "",
                "progress": None,
                "holders": [],
                "lifecycle": "on_demand",
                "roles": ["aggregator", "architect", "judge", "summarizer"],
            }
        ]

        async def hold_while_it_downloads() -> asyncio.Task[None]:
            async def hold() -> None:
                async with manager.hold("main", "test"):
                    pass

            task = asyncio.ensure_future(hold())
            await engine.reported.wait()
            return task

        task = c.portal.call(hold_while_it_downloads)
        row = c.get("/v1/airlie/engines").json()[0]
        assert row["state"] == "downloading" and row["detail"] == "org/m"
        assert row["progress"] == {"done_bytes": 5, "total_bytes": 20, "rate_bps": 2.0, "eta_s": 7.5}
        engine.gate.set()

        async def finish() -> None:
            await task

        c.portal.call(finish)
        assert c.get("/v1/airlie/engines").json()[0]["state"] == "ready"


def test_leases_hold_renew_and_release(
    tmp_path: Path, manager: Manager, engine: FakeEngine, clock: StuckClock
) -> None:
    with TestClient(serve(tmp_path, manager)) as c:
        made = c.post("/v1/airlie/engines/main/leases", json={"owner": "studio"})
        assert made.status_code == 200
        lease, ttl = made.json()["lease"], made.json()["ttl_s"]
        assert ttl == 120.0 and engine.up
        assert status(manager).holders == (f"lease:{lease}",)
        assert c.get("/v1/airlie/engines").json()[0]["holders"] == [f"lease:{lease}"]

        clock.now += 100
        assert c.put(f"/v1/airlie/engines/main/leases/{lease}").status_code == 204
        clock.now += 100  # 200 s since the grant, 100 since the renewal: inside the ttl
        c.portal.call(manager.reap)
        assert status(manager).holders == (f"lease:{lease}",)

        gone = c.delete(f"/v1/airlie/engines/main/leases/{lease}")
        assert gone.status_code == 204 and gone.content == b""
        assert status(manager).holders == ()


def test_an_expired_lease_is_released(tmp_path: Path, manager: Manager, clock: StuckClock) -> None:
    with TestClient(serve(tmp_path, manager)) as c:
        lease = c.post("/v1/airlie/engines/main/leases", json={"owner": "studio"}).json()["lease"]
        clock.now += 121
        c.portal.call(manager.reap)
        assert status(manager).holders == ()
        assert c.put(f"/v1/airlie/engines/main/leases/{lease}").status_code == 404


def test_status_changes_publish_an_engines_hint(tmp_path: Path, manager: Manager) -> None:
    app = serve(tmp_path, manager)
    seen: list[tuple[str, str | None]] = []
    feed = app.state.feed
    publish = feed.publish

    def spy(topic: str, key: str | None = None) -> None:
        seen.append((topic, key))
        publish(topic, key)

    feed.publish = spy
    with TestClient(app) as c:
        c.post("/v1/chat/completions", json=ASK)
    assert ("engines", "main") in seen


def test_the_engines_route_without_a_key_is_401(tmp_path: Path, manager: Manager) -> None:
    with TestClient(serve(tmp_path, manager, api_key="test-key"), client=ELSEWHERE) as c:
        assert c.get("/v1/airlie/engines").status_code == 401
        assert c.get("/v1/airlie/engines", headers=KEY).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/airlie/engines/main/leases"),
        ("PUT", "/v1/airlie/engines/main/leases/abc"),
        ("DELETE", "/v1/airlie/engines/main/leases/abc"),
    ],
)
def test_the_lease_routes_without_a_key_are_401(
    tmp_path: Path, manager: Manager, engine: FakeEngine, method: str, path: str
) -> None:
    with TestClient(serve(tmp_path, manager, api_key="test-key"), client=ELSEWHERE) as c:
        r = c.request(method, path, json={"owner": "x"} if method == "POST" else None)
        assert r.status_code == 401
        assert status(manager).holders == () and not engine.up


def test_a_lease_request_without_an_owner_is_422(
    tmp_path: Path, manager: Manager, engine: FakeEngine
) -> None:
    with TestClient(serve(tmp_path, manager)) as c:
        r = c.post("/v1/airlie/engines/main/leases", json={})
        assert r.status_code == 422
        assert r.json()["detail"][0]["loc"] == ["body", "owner"]
        assert not engine.up


def test_a_lease_on_an_unknown_engine_is_404(tmp_path: Path, manager: Manager) -> None:
    with TestClient(serve(tmp_path, manager)) as c:
        r = c.post("/v1/airlie/engines/nope/leases", json={"owner": "studio"})
        assert r.status_code == 404
        assert r.json()["detail"] == "unknown engine 'nope'; engines: main"


def test_renewing_or_deleting_an_unknown_lease_is_404(tmp_path: Path, manager: Manager) -> None:
    with TestClient(serve(tmp_path, manager)) as c:
        for call in (c.put, c.delete):
            r = call("/v1/airlie/engines/main/leases/ghost")
            assert r.status_code == 404
            assert r.json()["detail"] == "unknown lease 'ghost' on engine 'main'"


def test_a_failed_lease_start_is_a_503_naming_the_engine(
    tmp_path: Path, manager: Manager, engine: FakeEngine
) -> None:
    engine.fail = "no memory"
    with TestClient(serve(tmp_path, manager)) as c:
        r = c.post("/v1/airlie/engines/main/leases", json={"owner": "studio"})
        assert r.status_code == 503
        assert r.json() == {"error": "main failed: no memory", "engine": "main"}


async def call_app(app, *, fail_on: str) -> None:
    """Post a streamed chat straight to the ASGI app, then drop the client at the first `fail_on` message
    (`http.response.start`: before any body; `http.response.body`: mid-way)."""
    body = json.dumps({**ASK, "stream": True}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()  # the client stays quiet; the send below is what fails
        return {"type": "http.disconnect"}

    async def send(message) -> None:
        if message["type"] == fail_on:
            raise OSError("client went away")

    with contextlib.suppress(Exception):  # a dropped client ends the response in an error; the hold matters
        await app(scope, receive, send)


@pytest.mark.parametrize("fail_on", ["http.response.body", "http.response.start"])
async def test_a_stream_the_client_drops_releases_the_engine(
    tmp_path: Path, manager: Manager, fail_on: str
) -> None:
    sse = {"content-type": "text/event-stream"}
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, stream=SeesHolders(manager, []), headers=sse)
    )
    await asyncio.wait_for(call_app(serve(tmp_path, manager, transport=transport), fail_on=fail_on), 5)
    assert status(manager).state == "ready" and status(manager).holders == ()


class FailsToBuild(httpx.AsyncClient):
    def build_request(self, *a, **kw):
        raise RuntimeError("cannot build")


async def test_a_stream_that_fails_before_its_response_releases_the_engine(
    tmp_path: Path, manager: Manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FailsToBuild)
    await asyncio.wait_for(call_app(serve(tmp_path, manager), fail_on="never"), 5)
    assert status(manager).holders == ()


class BootFails(Manager):
    async def adopt(self) -> None:
        raise RuntimeError("probe blew up")


def test_a_failing_adopt_does_not_stop_the_pinned_engines_starting(
    tmp_path: Path, engine: FakeEngine
) -> None:
    engine.lifecycle = "pinned"
    m = BootFails([engine], budget_gb=100.0, clock=StuckClock())
    with TestClient(serve(tmp_path, m)) as c:

        async def until_up() -> None:
            while not engine.up:
                await asyncio.sleep(0)

        c.portal.call(asyncio.wait_for, until_up(), 5)
    assert engine.up


def test_serve_built_without_a_manager_manages_no_engine_in_tests(tmp_path: Path) -> None:
    config = cfg(tmp_path)
    config.engines["main"] = config.engines["main"].model_copy(update={"kind": "mlx_vlm"})
    with TestClient(create_app(config, transport=httpx.MockTransport(FakeUpstream()))) as c:
        assert c.get("/v1/airlie/engines").json() == []


def test_a_story_engine_built_inside_serve_holds_through_the_serve_manager(
    tmp_path: Path, manager: Manager, engine: FakeEngine
) -> None:
    from airlie.story.factory import _engine

    base = cfg(tmp_path)
    c = base.model_copy(
        update={"engines": {"main": base.engines["main"].model_copy(update={"kind": "mlx_vlm"})}}
    )
    with TestClient(serve(tmp_path, manager)) as client:

        async def held() -> tuple[str, ...]:
            async with _engine(c)._holder.hold("main", "role:architect"):  # type: ignore[attr-defined]
                return status(manager).holders

        assert client.portal.call(held) == ("role:architect",)
        assert engine.up


def test_a_chat_to_an_engine_started_out_of_band_adopts_it_and_answers(
    tmp_path: Path, manager: Manager, engine: FakeEngine
) -> None:
    with TestClient(serve(tmp_path, manager)) as c:
        engine.up = True  # `airlie engine up main` while serve runs: a second start would say so
        engine.fail = "engine 'main' is already running (pid 4242); use restart"
        r = c.post("/v1/chat/completions", json=ASK)
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "hi"
        assert status(manager).state == "ready"
