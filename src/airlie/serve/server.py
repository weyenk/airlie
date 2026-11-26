"""The process `airlie serve` runs: uvicorn's server, plus closing the live feed when shutdown begins.

uvicorn stops accepting connections, then waits for every open one to finish, and only then runs the app's
shutdown hook. A `/v1/events` stream never finishes on its own, so without this a Ctrl-C or SIGTERM would wait
for the browser to leave (launchd would end it with SIGKILL after its exit timeout, skipping the hooks that
close the databases). Chat turns in flight are stopped (each saves its partial reply as interrupted); other
requests, a story in flight, still drain as before."""

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
        self.chat_busy = app.state.chat_busy

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self.feed.close()  # every open stream ends, before uvicorn starts waiting for connections
        for (
            listener
        ) in self.servers:  # stop accepting first: a turn accepted after the stop below would be missed
            listener.close()
        self.chat_busy.stop_all()  # a hung engine must not hold the wait (max_steps x 600 s)
        await super().shutdown(sockets)


def run(app: FastAPI, *, host: str, port: int) -> None:
    """What `uvicorn.run(app, ...)` does for one process, with `Server`.

    `proxy_headers` stays off for every bind, a routable `host` such as `0.0.0.0` included, on purpose:
    uvicorn would replace `request.client` with the caller's own `X-Forwarded-For` for whoever sends it, and
    the peer address is the one fact the resolver trusts (airlie.auth.principal: `tailscale serve` on
    loopback, plus the proxies `auth.trusted_proxies` declares). Turning it on would make a forwarded header
    decide who a request is, which is the opposite of the guard. So the listen address only decides who can
    connect: binding `0.0.0.0` lets remote peers reach serve, they arrive with their own address, and nothing
    they assert about a second client is read. `airlie serve` refuses a routable bind with no API key and
    disables the fresh-install bootstrap (`bind_allows_bootstrap`) before calling here."""
    server = Server(app, host=host, port=port, log_level="info", proxy_headers=False)
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    if not server.started:
        sys.exit(STARTUP_FAILURE)  # as uvicorn.run does (a port in use): a supervisor sees the failure
