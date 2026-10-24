"""`airlie serve` must stop on the first Ctrl-C or SIGTERM even with a `/v1/events` stream open: uvicorn waits
for open connections before it runs the app's own shutdown hook, so the live feed is closed when uvicorn
begins to shut down (`Server.shutdown`), not by that hook."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from airlie.serve.app import create_app
from airlie.serve.server import Server, run
from tests.unit.test_serve import FakeUpstream, cfg


async def test_shutdown_ends_an_open_event_stream_so_the_server_can_stop(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    server = Server(app, host="127.0.0.1", port=0, log_level="warning")
    serving = asyncio.ensure_future(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            url = f"http://127.0.0.1:{port}/v1/events"
            async with (
                httpx.AsyncClient() as client,
                client.stream("GET", url, headers={"Authorization": "Bearer k"}) as response,
            ):
                lines = response.aiter_lines()
                assert await anext(lines) == ": connected"
                assert app.state.feed.subscribers == 1
                # what the first Ctrl-C or SIGTERM sets (the handler itself would re-raise the signal on exit)
                server.should_exit = True
                assert [line async for line in lines] == [""]  # the stream ends: nothing but its blank line
            await serving  # and with it the server: it was not left waiting for the stream
    finally:
        server.force_exit = True  # a failed run must not leave the server behind
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
    assert app.state.feed.subscribers == 0


def test_run_exits_with_uvicorns_startup_failure_code_when_the_server_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As `uvicorn.run` does for a port in use: a supervisor (launchd) sees the failure."""
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    monkeypatch.setattr(Server, "run", lambda self, sockets=None: None)  # never starts: started stays False
    with pytest.raises(SystemExit) as stop:
        run(app, host="127.0.0.1", port=0)
    assert stop.value.code == 3
