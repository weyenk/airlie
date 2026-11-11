"""The manager: one start for concurrent holds, room made by stopping idle on-demand resources, waiting with a
reason, reaping. Fake resources and a fake clock; no real time."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from airlie.engine.types import EngineError
from airlie.manager.core import Manager, Report, Status
from airlie.serve.bridge.clock import FakeClock


class FakeResource:
    kind: Literal["engine", "vm"] = "engine"

    def __init__(
        self,
        name: str,
        footprint_gb: float = 10.0,
        *,
        lifecycle: Literal["pinned", "on_demand"] = "on_demand",
        idle_unload_s: float = 600.0,
    ) -> None:
        self.name = name
        self.footprint_gb = footprint_gb
        self.lifecycle: Literal["pinned", "on_demand"] = lifecycle
        self.idle_unload_s = idle_unload_s
        self.starts = 0
        self.stops = 0
        self.up = False
        self.gate: asyncio.Event | None = None  # holds a start open until set
        self.fail: str | None = None  # a start raises EngineError(this)

    async def running(self) -> bool:
        return self.up

    async def start(self, report: Report) -> None:
        self.starts += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise EngineError(self.fail)
        self.up = True

    async def stop(self) -> None:
        self.stops += 1
        self.up = False


def manager(*resources: FakeResource, budget_gb: float = 100.0, wait_timeout_s: float = 1800.0, **kw):
    clock = FakeClock()
    return Manager(resources, budget_gb=budget_gb, clock=clock, wait_timeout_s=wait_timeout_s, **kw), clock


def state(m: Manager, name: str) -> Status:
    return next(s for s in m.status() if s.name == name)


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def test_concurrent_holds_share_one_start() -> None:
    r = FakeResource("a")
    r.gate = asyncio.Event()
    m, _ = manager(r)
    entered: list[str] = []

    async def use(owner: str) -> None:
        async with m.hold("a", owner):
            entered.append(owner)

    tasks = [asyncio.create_task(use("x")), asyncio.create_task(use("y"))]
    await settle()
    assert entered == [] and r.starts == 1
    r.gate.set()
    await asyncio.gather(*tasks)
    assert sorted(entered) == ["x", "y"] and r.starts == 1


async def test_a_hold_lists_its_owner_while_inside() -> None:
    m, _ = manager(FakeResource("a"))
    async with m.hold("a", "chat:1"):
        assert state(m, "a").holders == ("chat:1",)
        assert state(m, "a").state == "ready"
    assert state(m, "a").holders == ()


async def test_an_idle_on_demand_resource_is_stopped_after_its_idle_time() -> None:
    r = FakeResource("a", idle_unload_s=600.0)
    m, clock = manager(r)
    async with m.hold("a", "o"):
        pass
    clock.now += 599.0
    assert await m.reap() == [] and r.stops == 0
    clock.now += 1.0  # exactly the idle time: stopped
    assert await m.reap() == ["a"]
    assert r.stops == 1 and state(m, "a").state == "stopped"


async def test_a_pinned_resource_is_never_reaped() -> None:
    r = FakeResource("a", lifecycle="pinned")
    m, clock = manager(r)
    async with m.hold("a", "o"):
        pass
    clock.now += 10_000
    assert await m.reap() == [] and r.stops == 0


async def test_a_held_resource_is_not_reaped() -> None:
    r = FakeResource("a")
    m, clock = manager(r)
    async with m.hold("a", "o"):
        clock.now += 10_000
        assert await m.reap() == []
    assert r.stops == 0


async def test_a_held_resource_is_not_evicted_to_make_room() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=15)
    got = asyncio.Event()

    async def want_b() -> None:
        async with m.hold("b", "later"):
            got.set()

    async with m.hold("a", "busy"):
        task = asyncio.create_task(want_b())
        await settle()
        assert not got.is_set() and a.stops == 0 and b.starts == 0
        assert state(m, "b").state == "waiting"
    await asyncio.wait_for(task, 5)
    assert a.stops == 1 and b.starts == 1


async def test_room_is_made_by_stopping_the_least_recently_used_idle() -> None:
    a, b, c = FakeResource("a", 10), FakeResource("b", 10), FakeResource("c", 15)
    m, clock = manager(a, b, c, budget_gb=30)
    clock.now = 1.0
    async with m.hold("a", "o"):
        pass
    clock.now = 2.0
    async with m.hold("b", "o"):
        pass
    clock.now = 3.0
    async with m.hold("c", "o"):
        pass
    assert (a.stops, b.stops) == (1, 0)
    assert state(m, "a").state == "stopped" and state(m, "b").state == "ready"


async def test_touch_makes_a_resource_more_recent() -> None:
    a, b, c = FakeResource("a", 10), FakeResource("b", 10), FakeResource("c", 15)
    m, clock = manager(a, b, c, budget_gb=30)
    clock.now = 1.0
    async with m.hold("a", "o"):
        pass
    clock.now = 2.0
    async with m.hold("b", "o"):
        pass
    clock.now = 3.0
    m.touch("a")
    async with m.hold("c", "o"):
        pass
    assert (a.stops, b.stops) == (0, 1)


async def test_a_pinned_resource_is_not_evicted() -> None:
    a, b = FakeResource("a", 10, lifecycle="pinned"), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=15, wait_timeout_s=5)
    async with m.hold("a", "o"):
        pass
    with pytest.raises(EngineError):
        async with m.hold("b", "o"):
            pass
    assert a.stops == 0


async def test_waits_with_a_reason_then_starts_when_a_holder_releases() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=15)
    release = asyncio.Event()
    inside = asyncio.Event()

    async def holder() -> None:
        async with m.hold("a", "chat:1"):
            inside.set()
            await release.wait()

    h = asyncio.create_task(holder())
    await inside.wait()

    async def want() -> None:
        async with m.hold("b", "story:2"):
            pass

    w = asyncio.create_task(want())
    await settle()
    s = state(m, "b")
    assert s.state == "waiting" and s.detail == "waiting for memory: chat:1"
    release.set()
    await asyncio.wait_for(asyncio.gather(h, w), 5)
    assert b.starts == 1 and a.stops == 1


async def test_waiting_gives_up_after_wait_timeout() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    m, clock = manager(a, b, budget_gb=15, wait_timeout_s=20)
    async with m.hold("a", "chat:1"):
        with pytest.raises(EngineError, match=r"b: gave up waiting for memory after 20s"):
            async with m.hold("b", "o"):
                pass
        assert 20.0 <= clock.now < 22.0
        assert state(m, "b").state == "stopped"  # not left "waiting"
    assert b.starts == 0


async def test_a_dead_engine_is_failed_and_the_next_hold_restarts_it() -> None:
    r = FakeResource("a")
    m, _ = manager(r)
    async with m.hold("a", "o"):
        pass
    r.up = False  # it died
    seen: list[Status] = []
    m._on_change = seen.append
    async with m.hold("a", "o"):
        assert r.starts == 2
    assert ("failed", "exited") in [(s.state, s.detail) for s in seen]


async def test_a_start_that_raises_leaves_the_resource_failed_and_raises_to_the_holder() -> None:
    r = FakeResource("a")
    r.fail = "no weights"
    m, _ = manager(r)
    with pytest.raises(EngineError, match="no weights"):
        async with m.hold("a", "o"):
            raise AssertionError("must not enter")
    s = state(m, "a")
    assert s.state == "failed" and s.detail == "no weights" and s.holders == ()
    r.fail = None
    async with m.hold("a", "o"):
        pass
    assert state(m, "a").state == "ready"


async def test_releasing_a_hold_notifies_a_waiter_and_records_last_used() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    m, clock = manager(a, b, budget_gb=15)
    clock.now = 5.0
    cm = m.hold("a", "o")
    await cm.__aenter__()
    waiter = asyncio.ensure_future(m._changed.wait())
    await settle()
    assert not waiter.done()
    clock.now = 50.0
    await cm.__aexit__(None, None, None)
    await settle()
    assert waiter.done()
    clock.now = 50.0 + 599.0
    assert await m.reap() == []  # idle counted from the release, not the start
    clock.now = 50.0 + 600.0
    assert await m.reap() == ["a"]


async def test_adopt_marks_running_resources_ready_without_starting() -> None:
    a, b = FakeResource("a"), FakeResource("b")
    a.up = True
    m, clock = manager(a, b)
    clock.now = 7.0
    await m.adopt()
    assert state(m, "a").state == "ready" and state(m, "b").state == "stopped"
    assert a.starts == 0 and state(m, "a").since == 7.0
    clock.now = 7.0 + 600.0
    assert await m.reap() == ["a"]  # adoption counted as use at that time


async def test_start_pinned_starts_only_pinned_resources() -> None:
    p, o = FakeResource("p", lifecycle="pinned"), FakeResource("o")
    m, _ = manager(p, o)
    await m.start_pinned()
    assert (p.starts, o.starts) == (1, 0)
    assert state(m, "p").holders == ()


async def test_status_changes_are_reported() -> None:
    seen: list[tuple[str, str]] = []
    m, _ = manager(FakeResource("a"), on_change=lambda s: seen.append((s.name, s.state)))
    async with m.hold("a", "o"):
        pass
    assert [s for n, s in seen if n == "a"][:2] == ["starting", "ready"]


async def test_a_start_reports_progress_through_the_status() -> None:
    from airlie.models_download import DownloadProgress

    p = DownloadProgress(5, 10, 1.0, 5.0)

    class Reporting(FakeResource):
        async def start(self, report: Report) -> None:
            report("downloading", "weights", p)
            await asyncio.sleep(0)
            self.up = True

    seen: list[Status] = []
    m, _ = manager(Reporting("a"), on_change=seen.append)
    async with m.hold("a", "o"):
        pass
    d = next(s for s in seen if s.state == "downloading")
    assert d.progress is p and d.detail == "weights"
    assert state(m, "a").progress is None


async def test_a_failed_stop_during_eviction_marks_failed_and_frees_the_slot_for_the_waiter() -> None:
    class BadStop(FakeResource):
        async def stop(self) -> None:
            raise RuntimeError("stuck")

    a, b = BadStop("a", 10), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=15)
    async with m.hold("a", "o"):
        pass
    async with m.hold("b", "o"):
        pass
    assert state(m, "a").state == "failed" and "stuck" in state(m, "a").detail


async def test_run_reaps_every_interval() -> None:
    r = FakeResource("a", idle_unload_s=60)
    m, clock = manager(r)
    async with m.hold("a", "o"):
        pass
    task = asyncio.create_task(m.run(every_s=30))
    for _ in range(20):
        await asyncio.sleep(0)
        if r.stops:
            break
    task.cancel()
    assert r.stops == 1 and 60 <= clock.now <= 90
    assert clock.sleeps[:2] == [30, 30]
