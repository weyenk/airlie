"""The process `airlie serve` runs: uvicorn's server, plus closing the live feed when shutdown begins.

uvicorn stops accepting connections, then waits for every open one to finish, and only then runs the app's
shutdown hook. A `/v1/events` stream never finishes on its own, so without this a Ctrl-C or SIGTERM would wait
for the browser to leave (launchd would end it with SIGKILL after its exit timeout, skipping the hooks that
close the databases). Other requests, a chat or a story in flight, still drain as before."""

from __future__ import annotations

import contextlib
import socket
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI
from uvicorn.config import STARTUP_FAILURE


class Server(uvicorn.Server):
    """`uvicorn.Server` for an app from `create_app`. `shutdown` runs on the event loop (the signal handler
    only sets `should_exit`), so closing the feed there is not a call from signal context."""

    def __init__(self, app: FastAPI, **options: Any) -> None:
        super().__init__(uvicorn.Config(app, **options))
        self.feed = app.state.feed

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self.feed.close()  # every open stream ends, before uvicorn starts waiting for connections
        await super().shutdown(sockets)


def run(app: FastAPI, *, host: str, port: int) -> None:
    """What `uvicorn.run(app, ...)` does for one process, with `Server`. proxy_headers is off: the resolver
    reads the real peer to tell tailscale serve (loopback, with the device's tailnet address forwarded) from
    the Mac itself; uvicorn's default would rewrite it to X-Forwarded-For."""
    server = Server(app, host=host, port=port, log_level="info", proxy_headers=False)
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    if not server.started:
        sys.exit(STARTUP_FAILURE)  # as uvicorn.run does (a port in use): a supervisor sees the failure
