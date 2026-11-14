"""A LeaseHolder against serve's real lease routes: renewals keep the engine held past the idle limit, and
leaving the hold deletes the lease. The holder talks to the app through an ASGI transport; one clock drives
the manager's expiry (it moves only when the holder's renewal ticks sleep) and the holder's ticks."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from airlie.manager.client import LeaseHolder
from airlie.manager.core import Manager
from airlie.serve.app import create_app
from airlie.serve.bridge.clock import FakeClock
from tests.support.serve import FakeEngine, StuckClock, cfg
from tests.unit.serve_helpers import TestClient

TTL_S = 120.0  # cfg.manager.lease_ttl_s, the default


class Ticks(FakeClock):
    """The holder's clock: sleeping lets `world` (the manager's clock) pass that long."""

    def __init__(self, world: StuckClock) -> None:
        super().__init__()
        self.world = world

    async def sleep(self, seconds: float) -> None:
        self.world.now += seconds
        await asyncio.sleep(0)


def serve(tmp_path: Path) -> tuple[TestClient, Manager, FakeEngine, StuckClock]:
    engine, clock = FakeEngine("main"), StuckClock()
    manager = Manager([engine], budget_gb=100.0, clock=clock)
    app = create_app(
        cfg(tmp_path), manager=manager, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    return TestClient(app), manager, engine, clock


def holder_on(c: TestClient, world: StuckClock) -> LeaseHolder:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app, client=("127.0.0.1", 50000)))
    return LeaseHolder("http://serve.test", None, client, Ticks(world))


def holders(manager: Manager) -> tuple[str, ...]:
    return manager.status()[0].holders


def test_a_renewed_lease_keeps_the_engine_past_idle(tmp_path: Path) -> None:
    c, manager, engine, clock = serve(tmp_path)
    with c:

        async def hold_for_three_ttls() -> tuple[str, ...]:
            async with holder_on(c, clock).hold("main", "role:architect"):
                for _ in range(10_000):
                    if clock.now >= 3 * TTL_S:
                        break
                    await manager.reap()  # expires an unrenewed lease; stops an idle engine
                    await asyncio.sleep(0)
                return holders(manager)

        held = c.portal.call(hold_for_three_ttls)
        assert clock.now >= 3 * TTL_S
        assert len(held) == 1 and held[0].startswith("lease:")
        assert engine.up


def test_leaving_the_hold_deletes_the_lease(tmp_path: Path) -> None:
    c, manager, engine, clock = serve(tmp_path)
    with c:

        async def hold_once() -> tuple[str, ...]:
            async with holder_on(c, clock).hold("main", "role:architect"):
                return holders(manager)

        (lease,) = c.portal.call(hold_once)
        assert lease.startswith("lease:")
        assert holders(manager) == ()
        assert c.put(f"/v1/airlie/engines/main/leases/{lease.removeprefix('lease:')}").status_code == 404
