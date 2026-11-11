"""Every engine path goes through the manager: a role call holds its engine while it runs.

Inside serve the hold is the manager's own; from another process (the launchd story tick, `task run`) it is a
lease taken from serve over HTTP; with no serve running the engine is started here and left for serve's
reaper, which stops it once it is idle.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Protocol

import httpx

from airlie.config import AirlieConfig
from airlie.engine.process import EngineProcess
from airlie.engine.protocol import Engine, OnToken
from airlie.engine.types import (
    EngineCapabilities,
    EngineError,
    EngineHealth,
    EngineStats,
    GenRequest,
    GenResult,
    Message,
)
from airlie.manager.core import Manager
from airlie.manager.engines import start_or_join
from airlie.serve.bridge.clock import Clock, RealClock

log = logging.getLogger(__name__)

DEFAULT_SERVE_URL = "http://127.0.0.1:8090"


class Holder(Protocol):
    def hold(self, engine: str, owner: str) -> AbstractAsyncContextManager[None]: ...


_serving: Manager | None = None


@contextmanager
def serving(manager: Manager) -> Iterator[None]:
    """While serve runs, the manager every engine in this process holds through: stories and initiatives built
    inside serve are many calls deep, and none of them must start an engine behind the manager's back."""
    global _serving
    previous, _serving = _serving, manager
    try:
        yield
    finally:
        _serving = previous


class InProcessHolder:
    """Inside serve: the manager holds the engine itself (an engine it does not manage is left alone)."""

    def __init__(self, manager: Manager) -> None:
        self._manager = manager

    @asynccontextmanager
    async def hold(self, engine: str, owner: str) -> AsyncIterator[None]:
        if engine not in {s.name for s in self._manager.status()}:
            yield
            return
        async with self._manager.hold(engine, owner):
            yield


class LeaseHolder:
    """From another process: a lease on serve, renewed every ttl/3 while held and deleted on exit."""

    def __init__(self, serve_url: str, api_key: str | None, client: httpx.AsyncClient, clock: Clock) -> None:
        self._root = serve_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client
        self._clock = clock

    def _url(self, engine: str, lease: str | None = None) -> str:
        base = f"{self._root}/v1/airlie/engines/{engine}/leases"
        return base if lease is None else f"{base}/{lease}"

    async def _renew(self, engine: str, lease: str, every_s: float) -> None:
        while True:
            await self._clock.sleep(every_s)
            try:
                r = await self._client.put(self._url(engine, lease), headers=self._headers)
            except httpx.HTTPError as e:
                log.warning("lease %s on %s: renewal failed: %s", lease, engine, e)
                continue  # the next tick tries again; the ttl is three ticks
            if r.status_code == 404:
                log.warning("lease %s on %s is gone", lease, engine)
                return

    @asynccontextmanager
    async def hold(self, engine: str, owner: str) -> AsyncIterator[None]:
        try:
            r = await self._client.post(self._url(engine), json={"owner": owner}, headers=self._headers)
        except httpx.HTTPError as e:
            raise EngineError(f"{engine}: serve did not answer the lease request: {e}") from e
        if r.status_code != 200:
            raise EngineError(f"{engine}: {_why(r)}")
        body = r.json()
        lease: str = body["lease"]
        renewing = asyncio.ensure_future(self._renew(engine, lease, float(body["ttl_s"]) / 3))
        try:
            yield
        finally:
            renewing.cancel()
            with suppress(asyncio.CancelledError):
                await renewing
            try:
                await self._client.delete(self._url(engine, lease), headers=self._headers)
            except httpx.HTTPError as e:
                log.warning("lease %s on %s: release failed (it expires on its own): %s", lease, engine, e)


def _why(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"])
    except (ValueError, KeyError, TypeError):
        return f"serve answered {r.status_code}"


class LocalHolder:
    """No serve: start the engine here if it is down. Never stopped from here: serve's reaper does that."""

    def __init__(
        self,
        cfg: AirlieConfig,
        state_dir: Path,
        *,
        process_for: Callable[[str], EngineProcess] | None = None,
    ) -> None:
        self._cfg = cfg
        self._process_for = process_for or (lambda name: EngineProcess(cfg, name, state_dir=state_dir))
        self._starting: dict[str, asyncio.Lock] = {}

    async def _up(self, process: EngineProcess) -> None:
        if await process.prober.health_ok():
            return
        # another process may start it between the probe and the start: then it is waited on
        await start_or_join(process, self._cfg.manager.start_timeout_s)

    @asynccontextmanager
    async def hold(self, engine: str, owner: str) -> AsyncIterator[None]:
        process = self._process_for(engine)
        async with self._starting.setdefault(engine, asyncio.Lock()):  # one start at a time per engine
            await self._up(process)
        yield


class ManagedEngine:
    """`inner` with each generation held on its engine."""

    def __init__(self, inner: Engine, engine_name: str, holder: Holder) -> None:
        self._inner = inner
        self._engine_name = engine_name
        self._holder = holder
        self.capabilities: EngineCapabilities = inner.capabilities

    async def start(self) -> None:
        await self._inner.start()

    async def stop(self) -> None:
        await self._inner.stop()

    async def health(self) -> EngineHealth:
        return await self._inner.health()

    async def generate(self, req: GenRequest, on_token: OnToken | None = None) -> GenResult:
        async with self._holder.hold(self._engine_name, owner=f"role:{req.role}"):
            return await self._inner.generate(req, on_token)

    async def cancel(self, request_id: str) -> None:
        await self._inner.cancel(request_id)

    async def yes_no(self, role: str, messages: list[Message]) -> tuple[float, float]:
        async with self._holder.hold(self._engine_name, owner=f"role:{role}"):
            return await self._inner.yes_no(role, messages)

    def stats(self) -> EngineStats:
        return self._inner.stats()


class LazyHolder:
    """A holder chosen at its first hold, off the event loop: choosing probes serve (maybe this loop). A local
    choice is made again at every hold, so a caller that first found serve down takes leases once it answers
    (its starts then count in serve's budget, and serve sees its holds)."""

    def __init__(self, choose: Callable[[], Holder]) -> None:
        self._choose = choose
        self._chosen: Holder | None = None
        self._lock = asyncio.Lock()

    async def _holder(self) -> Holder:
        async with self._lock:
            if self._chosen is None or isinstance(self._chosen, LocalHolder):
                chosen = await asyncio.to_thread(self._choose)
                if not (isinstance(chosen, LocalHolder) and isinstance(self._chosen, LocalHolder)):
                    self._chosen = chosen  # still local: keep the first, whose start locks every hold shares
            return self._chosen

    @asynccontextmanager
    async def hold(self, engine: str, owner: str) -> AsyncIterator[None]:
        async with (await self._holder()).hold(engine, owner):
            yield


def serve_target(cfg: AirlieConfig, env: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    """Where serve is and the key it wants, as `airlie mcp` reads them."""
    from airlie.serve.bridge.session import resolve_serve_api_key

    e = os.environ if env is None else env
    return e.get("AIRLIE_URL") or DEFAULT_SERVE_URL, resolve_serve_api_key(cfg, env=e)


def holder_for(
    cfg: AirlieConfig,
    *,
    manager: Manager | None,
    env: Mapping[str, str] | None = None,
    transport: httpx.MockTransport | None = None,
    clock: Clock | None = None,
) -> Holder:
    """The manager when given (inside serve), else serve's lease routes when its /health answers, else here.
    `transport` is for tests: the one stand-in for serve's network."""
    manager = manager or _serving
    if manager is not None:
        return InProcessHolder(manager)
    url, key = serve_target(cfg, env)
    try:
        with httpx.Client(timeout=2.0, transport=transport) as probe:
            up = probe.get(f"{url.rstrip('/')}/health").status_code == 200
    except httpx.HTTPError:
        up = False
    if up:
        # a lease request waits for the engine to start: no read timeout
        client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0), transport=transport)
        return LeaseHolder(url, key, client, clock or RealClock())
    return LocalHolder(cfg, cfg.observability.state_dir.expanduser())
