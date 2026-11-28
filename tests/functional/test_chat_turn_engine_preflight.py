"""A turn whose engine cannot be reached says so instead of going quiet.

serve used to open the SSE stream before it talked to the engine, so an unreachable engine left the browser
with a 200 and no frames at all: nothing on screen, and nothing in the server log pointing at the engine. The
turn now asks the engine for its model list first, on a client of its own, and refuses with 502 before it
takes a thread, a run slot, or a VM.

The refusals drive a real TCP address (nothing is behind it, as on a network where the engine's host is down).
The healthy case is checked through a server socket that answers the probe, because a connecting client would
otherwise be left hanging — accepting is the turn's own job, not this test's.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.auth.principal import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.chat.engine_probe import EngineOffline, EngineProber
from airlie.serve.app import create_app
from tests.support.serve_chat import ScriptedUpstream, Text, chat_cfg
from tests.unit.serve_helpers import TestClient

ELSEWHERE = ("10.0.0.5", 40000)


def dead_url() -> str:
    """An engine address nothing answers: a port bound and released, so the refusal is fast, the way a down
    host on a reachable network looks to a client."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"


def client(
    tmp_path: Path, up: ScriptedUpstream, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> TestClient:
    """A server whose engine lives at `base_url`. With no `transport` the pre-flight reaches that address for
    real, while the turn's own answers still stream through `up`."""
    conf = chat_cfg(tmp_path)
    conf.engines["main"].base_url = base_url
    store = AuthStore(tmp_path / "auth.sqlite")
    pat = store.add_user("Pat", "member")
    app = create_app(conf, transport=transport or httpx.MockTransport(up), auth_store=store)
    cookie = {"Cookie": f"{SESSION_COOKIE}={store.make_session(pat.id, 30)}"}
    return TestClient(app, client=ELSEWHERE, headers=cookie)


def turn(c: TestClient) -> tuple[int, list[dict[str, Any]], float]:
    """Send one turn and report its status, its SSE frames, and how long the whole thing took."""
    msg = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}
    body = {"id": "t1", "kind": "plain", "messages": [msg]}
    started = time.monotonic()
    frames: list[dict[str, Any]] = []
    with c.stream("POST", "/v1/airlie/chat/turn", json=body) as r:
        if r.status_code != 200:
            r.read()
            return r.status_code, [], time.monotonic() - started
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            frames.append(json.loads(payload))
    return r.status_code, frames, time.monotonic() - started


def answering_engine() -> tuple[str, Callable[[], None]]:
    """A live engine on loopback: answers the pre-flight's model list with a model list. The turn itself never
    reaches it — the turn goes through the app's own faked transport — so this only has to look alive."""
    body = b'{"data": []}'

    class Engine(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/v1", server.shutdown


def test_an_unreachable_engine_is_refused_with_502_before_it_takes_anything(tmp_path: Path) -> None:
    up = ScriptedUpstream(Text("hi there"))
    c = client(tmp_path, up, dead_url(), transport=httpx.AsyncHTTPTransport())
    status, frames, took = turn(c)

    assert status == 502, f"a dead engine answered {status} with frames {frames}"
    assert took < 5.0, f"the turn hung for {took:.1f}s instead of reporting the engine"
    assert up.bodies == [], "an unreachable engine must not also be sent the turn"
    # nothing was taken for a turn that never ran: no thread row, so the next one is still fresh
    assert c.get("/v1/airlie/chat/threads").json() == []


def test_an_address_that_never_answers_is_refused_in_bounded_time(tmp_path: Path) -> None:
    """RFC 5737 leaves this address unassigned: nothing answers, so the probe's own bound is what ends it."""
    up = ScriptedUpstream(Text("hi there"))
    c = client(tmp_path, up, "http://192.0.2.1:8000/v1", transport=httpx.AsyncHTTPTransport())
    status, _frames, took = turn(c)

    assert status == 502
    assert took < 12.0, f"the probe waited {took:.1f}s; it must hold its own bound, not the engine's 600s"
    assert up.bodies == []


def test_a_reachable_engine_streams_and_its_probe_is_not_the_turns_call(tmp_path: Path) -> None:
    base_url, stop = answering_engine()
    scripted = ScriptedUpstream(Text("hi there"))
    c = client(tmp_path, scripted, base_url)
    status, frames, _ = turn(c)

    assert status == 200
    kinds = [f["type"] for f in frames]
    assert "error" not in kinds
    assert kinds[0] == "start" and "finish" in kinds
    assert "".join(f.get("delta", "") for f in frames if f["type"] == "text-delta") == "hi there"
    assert len(scripted.bodies) == 1, "the pre-flight runs on its own client, not the turn's upstream"
    stop()


def test_the_prober_treats_an_answer_with_a_status_as_reachable() -> None:
    """A server that answers 401 or 404 is up; only the transport failing means the turn cannot run."""

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "api key required"})

    async def ask() -> None:
        await EngineProber(httpx.MockTransport(refuse))("http://engine:8000/v1")

    asyncio.run(ask())  # no EngineOffline


def test_the_prober_reports_no_engine_at_all() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def ask() -> None:
        await EngineProber(httpx.MockTransport(down))("http://engine:8000/v1")

    with pytest.raises(EngineOffline) as raised:
        asyncio.run(ask())
    assert "http://engine:8000/v1/models" in str(raised.value)
