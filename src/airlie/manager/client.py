"""Holding a manager resource for the length of a call: inside serve the hold is the manager's own; from
another process it is a lease taken from serve over HTTP. airlie starts no inference server, so an engine call
holds nothing unless a resource of that name is registered.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager, suppress
from typing import Protocol

import httpx

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
from airlie.serve.bridge.clock import Clock

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


class _Lease:
    """The lease a hold has now: None while serve, restarted, has not granted the next one."""

    def __init__(self, lease: str, every_s: float) -> None:
        self.id: str | None = lease
        self.every_s = every_s


class LeaseHolder:
    """From another process: a lease on serve, renewed every ttl/3 while held and deleted on exit. A lease
    serve no longer knows (it restarted) is taken again for the same owner, asked for each tick till serve
    grants it."""

    def __init__(self, serve_url: str, api_key: str | None, client: httpx.AsyncClient, clock: Clock) -> None:
        self._root = serve_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client
        self._clock = clock

    def _url(self, engine: str, lease: str | None = None) -> str:
        base = f"{self._root}/v1/airlie/engines/{engine}/leases"
        return base if lease is None else f"{base}/{lease}"

    async def _take(self, engine: str, owner: str) -> _Lease:
        try:
            r = await self._client.post(self._url(engine), json={"owner": owner}, headers=self._headers)
        except httpx.HTTPError as e:
            raise EngineError(f"{engine}: serve did not answer the lease request: {e}") from e
        if r.status_code != 200:
            raise EngineError(f"{engine}: {_why(r)}")
        body = r.json()
        return _Lease(body["lease"], float(body["ttl_s"]) / 3)

    async def _renew(self, engine: str, owner: str, held: _Lease) -> None:
        while True:
            await self._clock.sleep(held.every_s)
            if held.id is not None:
                try:
                    r = await self._client.put(self._url(engine, held.id), headers=self._headers)
                except httpx.HTTPError as e:
                    log.warning("lease %s on %s: renewal failed: %s", held.id, engine, e)
                    continue  # the next tick tries again; the ttl is three ticks
                if r.status_code != 404:
                    continue
                log.warning("lease %s on %s is gone (serve restarted?): taking a new one", held.id, engine)
                held.id = None
            try:
                fresh = await self._take(engine, owner)
            except EngineError as e:
                log.warning("a new lease on %s failed (the next tick asks again): %s", engine, e)
                continue
            held.id, held.every_s = fresh.id, fresh.every_s

    @asynccontextmanager
    async def hold(self, engine: str, owner: str) -> AsyncIterator[None]:
        held = await self._take(engine, owner)
        renewing = asyncio.ensure_future(self._renew(engine, owner, held))
        try:
            yield
        finally:
            renewing.cancel()
            with suppress(asyncio.CancelledError):
                await renewing
            if held.id is not None:
                try:
                    await self._client.delete(self._url(engine, held.id), headers=self._headers)
                except httpx.HTTPError as e:
                    log.warning(
                        "lease %s on %s: release failed (it expires on its own): %s", held.id, engine, e
                    )


def _why(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"])
    except (ValueError, KeyError, TypeError):
        return f"serve answered {r.status_code}"


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
