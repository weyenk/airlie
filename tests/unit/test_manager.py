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
        self.stop_gate: asyncio.Event | None = None  # holds a stop open until set
        self.stop_error: str | None = None  # a stop raises (the process stays up)

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
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if self.stop_error is not None:
            raise RuntimeError(self.stop_error)
        self.up = False


class StuckClock(FakeClock):
    """A clock whose sleeps never finish: a waiter wakes only on a notification."""

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.Event().wait()


class StopsAfter(FakeClock):
    """Ends `Manager.run` after `n` sleeps."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.n = n

    async def sleep(self, seconds: float) -> None:
        if len(self.sleeps) >= self.n:
            raise asyncio.CancelledError
        await super().sleep(seconds)


def manager(
    *resources: FakeResource,
    budget_gb: float = 100.0,
    wait_timeout_s: float = 1800.0,
    clock: FakeClock | None = None,
    **kw,
):
    clock = clock or FakeClock()
    return Manager(resources, budget_gb=budget_gb, clock=clock, wait_timeout_s=wait_timeout_s, **kw), clock


def state(m: Manager, name: str) -> Status:
    return next(s for s in m.status() if s.name == name)


async def settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
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
    clock.now += 600.0  # exactly the idle time: stopped
    assert await m.reap() == ["a"]
    assert r.stops == 1 and state(m, "a").state == "stopped"


async def test_a_resource_idle_for_less_than_its_idle_time_is_not_reaped() -> None:
    r = FakeResource("a", idle_unload_s=600.0)
    m, clock = manager(r)
    async with m.hold("a", "o"):
        pass
    clock.now += 599.0
    assert await m.reap() == [] and r.stops == 0


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


async def test_a_waiting_resource_names_every_blocking_holder_joined_by_comma_space() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=15, wait_timeout_s=5)
    release = asyncio.Event()

    async def holder(owner: str) -> None:
        async with m.hold("a", owner):
            await release.wait()

    async def want() -> None:
        async with m.hold("b", "story:2"):
            pass

    holders = [asyncio.create_task(holder("chat:1")), asyncio.create_task(holder("role:reviewer"))]
    await settle()
    w = asyncio.create_task(want())
    await settle()

    assert state(m, "b").detail == "waiting for memory: chat:1, role:reviewer"
    release.set()
    await asyncio.wait_for(asyncio.gather(*holders, w), 5)


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
    clock.now = 50.0 + 600.0
    assert await m.reap() == ["a"]  # idle counted from the release (50), not the start (5)


async def test_idle_time_counts_from_the_release() -> None:
    r = FakeResource("a")
    m, clock = manager(r)
    clock.now = 5.0
    async with m.hold("a", "o"):
        clock.now = 50.0
    clock.now = 50.0 + 599.0
    assert await m.reap() == []


async def test_adopt_marks_running_resources_ready_without_starting() -> None:
    a, b = FakeResource("a"), FakeResource("b")
    a.up = True
    m, clock = manager(a, b)
    clock.now = 7.0
    await m.adopt()
    assert state(m, "a").state == "ready" and state(m, "b").state == "stopped"
    assert a.starts == 0 and state(m, "a").since == 7.0


async def test_adoption_counts_as_use_for_the_idle_clock() -> None:
    a = FakeResource("a")
    a.up = True
    m, clock = manager(a)
    clock.now = 7.0
    await m.adopt()
    clock.now = 7.0 + 599.0
    assert await m.reap() == []


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


async def _hold(m: Manager, name: str, owner: str = "o") -> None:
    async with m.hold(name, owner):
        pass


async def test_a_failed_stop_during_eviction_keeps_counting_for_memory() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    a.stop_error = "stuck"
    m, _ = manager(a, b, budget_gb=15, wait_timeout_s=5)
    await _hold(m, "a")
    with pytest.raises(EngineError, match="gave up waiting"):
        await _hold(m, "b")
    assert b.starts == 0
    assert state(m, "a").state == "failed" and "stuck" in state(m, "a").detail


async def test_a_resource_whose_stop_failed_stops_counting_once_it_is_seen_dead() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    a.stop_error = "stuck"
    m, _ = manager(a, b, budget_gb=15)
    await _hold(m, "a")
    waiting = asyncio.create_task(_hold(m, "b"))
    await settle()
    assert not waiting.done()
    a.up = False
    await asyncio.wait_for(waiting, 5)
    assert b.starts == 1 and state(m, "a").state == "stopped"


async def test_holding_a_resource_whose_stop_failed_and_is_alive_raises() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    a.stop_error = "stuck"
    m, _ = manager(a, b, budget_gb=15, wait_timeout_s=1)
    await _hold(m, "a")
    with pytest.raises(EngineError):
        await _hold(m, "b")
    with pytest.raises(EngineError, match="stop failed: stuck"):
        await _hold(m, "a")
    assert a.starts == 1


async def test_run_reaps_every_interval() -> None:
    r = FakeResource("a", idle_unload_s=60)
    clock = StopsAfter(2)
    m, _ = manager(r, clock=clock)
    await _hold(m, "a")
    with pytest.raises(asyncio.CancelledError):
        await m.run(every_s=30)
    assert r.stops == 1 and clock.sleeps == [30, 30]


async def test_reap_awaits_its_before_hook_first() -> None:
    r = FakeResource("a", idle_unload_s=60)
    m, clock = manager(r)
    await _hold(m, "a")
    clock.now = 100
    order: list[str] = []

    async def before() -> None:
        order.append(f"before, a stops={r.stops}")

    m.before_reap = before
    await m.reap()
    assert order == ["before, a stops=0"] and r.stops == 1


async def test_run_survives_a_reap_that_raises() -> None:
    r = FakeResource("a", idle_unload_s=60)
    clock = StopsAfter(2)
    m, _ = manager(r, clock=clock)
    await _hold(m, "a")
    calls: list[int] = []

    async def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("sweep blew up")

    m.before_reap = flaky
    with pytest.raises(asyncio.CancelledError):
        await m.run(every_s=30)
    assert len(calls) == 2 and r.stops == 1


async def test_watch_hears_every_status_change_beside_the_constructor_callback() -> None:
    r = FakeResource("a")
    first: list[str] = []
    second: list[str] = []
    m, _ = manager(r, on_change=lambda s: first.append(s.state))
    m.watch(lambda s: second.append(s.state))
    await _hold(m, "a")
    assert first == second and "ready" in second


async def test_status_carries_the_resources_lifecycle() -> None:
    m, _ = manager(FakeResource("a", lifecycle="pinned"), FakeResource("b"))
    assert [(s.name, s.lifecycle) for s in m.status()] == [("a", "pinned"), ("b", "on_demand")]


# ---- races ----


async def test_a_hold_on_a_resource_being_evicted_waits_for_the_stop_to_finish() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=15)
    await _hold(m, "a")
    a.stop_gate = asyncio.Event()
    evicting = asyncio.create_task(_hold(m, "b"))
    await settle()
    assert state(m, "a").state == "stopping"
    again = asyncio.create_task(_hold(m, "a", "y"))
    await settle()
    assert a.starts == 1 and a.stops == 1  # no start while the stop is open
    a.stop_gate.set()
    await asyncio.wait_for(asyncio.gather(evicting, again), 5)
    assert a.starts == 2 and a.up and state(m, "a").holders == ()


async def test_a_hold_arriving_while_reap_stops_does_not_overlap_the_stop() -> None:
    a = FakeResource("a", idle_unload_s=1)
    m, clock = manager(a)
    await _hold(m, "a")
    clock.now += 5
    a.stop_gate = asyncio.Event()
    reaping = asyncio.create_task(m.reap())
    await settle()
    again = asyncio.create_task(_hold(m, "a", "y"))
    await settle()
    assert a.starts == 1
    a.stop_gate.set()
    await asyncio.wait_for(asyncio.gather(reaping, again), 5)
    assert a.starts == 2 and a.up


async def test_a_resource_being_started_counts_for_memory() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    a.gate = asyncio.Event()
    m, _ = manager(a, b, budget_gb=15)
    starting = asyncio.create_task(_hold(m, "a"))
    await settle()
    waiting = asyncio.create_task(_hold(m, "b"))
    await settle()
    assert b.starts == 0 and state(m, "b").state == "waiting"
    a.gate.set()
    await asyncio.wait_for(asyncio.gather(starting, waiting), 5)
    assert a.stops == 1 and b.starts == 1


async def test_a_resource_being_stopped_counts_for_memory() -> None:
    a, b, c = FakeResource("a", 10), FakeResource("b", 10), FakeResource("c", 10)
    m, _ = manager(a, b, c, budget_gb=15)
    await _hold(m, "a")
    a.stop_gate = asyncio.Event()
    first = asyncio.create_task(_hold(m, "b"))
    await settle()
    second = asyncio.create_task(_hold(m, "c"))
    await settle()
    assert c.starts == 0 and b.starts == 0  # a's memory is not free until its stop ends
    a.stop_gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert b.stops == 1 and c.starts == 1


async def test_a_failed_start_wakes_a_waiter_behind_it() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    a.gate = asyncio.Event()
    a.fail = "no weights"
    m, _ = manager(a, b, budget_gb=15, clock=StuckClock())
    starting = asyncio.create_task(_hold(m, "a"))
    await settle()
    waiting = asyncio.create_task(_hold(m, "b"))
    await settle()
    assert state(m, "b").state == "waiting"
    a.gate.set()
    with pytest.raises(EngineError):
        await starting
    await asyncio.wait_for(waiting, 5)
    assert b.starts == 1


async def test_a_release_wakes_a_waiter_without_any_timer() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=15, clock=StuckClock())
    cm = m.hold("a", "o")
    await cm.__aenter__()
    waiting = asyncio.create_task(_hold(m, "b"))
    await settle()
    assert not waiting.done()
    await cm.__aexit__(None, None, None)
    await asyncio.wait_for(waiting, 5)


async def test_a_resource_with_a_hold_in_flight_is_not_reaped() -> None:
    a = FakeResource("a", idle_unload_s=1)
    m, clock = manager(a)
    await _hold(m, "a")
    clock.now += 5
    entered = asyncio.Event()
    leave = asyncio.Event()

    async def hold_in_flight() -> None:
        async with m.hold("a", "y"):
            entered.set()
            await leave.wait()

    task = asyncio.create_task(hold_in_flight())
    await entered.wait()
    assert await m.reap() == []
    leave.set()
    await task
    assert a.stops == 0


async def test_a_resource_whose_hold_is_between_its_check_and_its_entry_is_not_reaped() -> None:
    a = FakeResource("a", idle_unload_s=1)
    m, clock = manager(a)
    await _hold(m, "a")
    clock.now += 5
    gate = asyncio.Event()

    async def slow_running() -> bool:
        await gate.wait()
        return True

    a.running = slow_running  # type: ignore[method-assign]
    task = asyncio.create_task(_hold(m, "a", "y"))
    await settle()
    assert await m.reap() == []
    gate.set()
    await task
    assert a.stops == 0


async def test_an_exception_in_the_hold_body_still_releases_the_holder() -> None:
    m, _ = manager(FakeResource("a"))
    with pytest.raises(ValueError):
        async with m.hold("a", "o"):
            raise ValueError("boom")
    assert state(m, "a").holders == ()


async def test_start_pinned_starts_the_rest_when_one_fails() -> None:
    p, q = FakeResource("p", lifecycle="pinned"), FakeResource("q", lifecycle="pinned")
    p.fail = "no weights"
    m, _ = manager(p, q)
    await m.start_pinned()
    assert state(m, "p").state == "failed" and state(m, "p").detail == "no weights"
    assert state(m, "q").state == "ready"


async def test_a_manager_built_with_defaults_gives_up_waiting_after_1800_seconds() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    clock = FakeClock()
    m = Manager([a, b], budget_gb=15, clock=clock)
    async with m.hold("a", "chat:1"):
        with pytest.raises(EngineError, match=r"b: gave up waiting for memory after 1800s"):
            async with m.hold("b", "o"):
                pass


async def test_status_carries_the_resources_kind() -> None:
    m, _ = manager(FakeResource("a"))
    assert state(m, "a").kind == "engine"


async def test_the_starting_status_has_no_detail() -> None:
    seen: list[Status] = []
    m, _ = manager(FakeResource("a"), on_change=seen.append)
    await _hold(m, "a")
    assert [(s.state, s.detail) for s in seen[:2]] == [("starting", ""), ("ready", "")]


async def test_a_resource_stopped_by_reap_is_reported_stopped_once() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    seen: list[str] = []
    m, clock = manager(
        a, b, budget_gb=15, on_change=lambda s: seen.append(s.state) if s.name == "a" else None
    )
    await _hold(m, "a")
    clock.now += 600.0
    await m.reap()
    seen.clear()
    await _hold(m, "b")  # makes room: nothing left to forget on a
    assert seen == []


async def test_a_resource_that_fits_the_budget_exactly_starts_without_evicting() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=20)
    await _hold(m, "a")
    await _hold(m, "b")
    assert a.stops == 0 and b.starts == 1


async def test_a_waiting_resource_reports_its_reason_once_however_often_it_re_checks() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    waiting: list[str] = []
    m, _ = manager(
        a,
        b,
        budget_gb=15,
        wait_timeout_s=5,
        on_change=lambda s: waiting.append(s.detail) if s.name == "b" and s.state == "waiting" else None,
    )
    async with m.hold("a", "chat:1"):
        with pytest.raises(EngineError):
            await _hold(m, "b")
    assert waiting == ["waiting for memory: chat:1"]


async def test_a_waiting_resource_follows_its_blockers_as_they_change() -> None:
    a, b = FakeResource("a", 10), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=15, wait_timeout_s=5)
    release = asyncio.Event()

    async def holder(owner: str) -> None:
        async with m.hold("a", owner):
            await release.wait()

    first = asyncio.create_task(holder("chat:1"))
    await settle()
    w = asyncio.create_task(_hold(m, "b"))
    await settle()
    second = asyncio.create_task(holder("chat:2"))
    await settle()
    assert state(m, "b").detail == "waiting for memory: chat:1, chat:2"
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second, w), 5)


async def test_a_start_that_raises_with_no_message_is_failed_with_the_exception_name() -> None:
    r = FakeResource("a")
    r.fail = ""
    m, _ = manager(r)
    with pytest.raises(EngineError):
        await _hold(m, "a")
    assert state(m, "a").detail == "EngineError"


async def test_start_pinned_holds_a_resource_as_start() -> None:
    seen: list[tuple[str, ...]] = []
    m, _ = manager(FakeResource("p", lifecycle="pinned"), on_change=lambda s: seen.append(s.holders))
    await m.start_pinned()
    assert ("start",) in seen


async def test_start_pinned_leaves_a_ready_pinned_resource_alone() -> None:
    p = FakeResource("p", lifecycle="pinned")
    seen: list[Status] = []
    m, _ = manager(p)
    await m.start_pinned()
    m._on_change = seen.append
    await m.start_pinned()
    assert seen == [] and p.starts == 1


async def test_reap_goes_on_past_a_resource_it_may_not_stop() -> None:
    p, r = FakeResource("p", lifecycle="pinned"), FakeResource("r", idle_unload_s=60)
    m, clock = manager(p, r)
    await _hold(m, "p")
    await _hold(m, "r")
    clock.now += 60.0
    assert await m.reap() == ["r"]


async def test_reap_goes_on_past_a_resource_not_idle_long_enough() -> None:
    fresh, old = FakeResource("fresh", idle_unload_s=600), FakeResource("old", idle_unload_s=60)
    m, clock = manager(fresh, old)
    await _hold(m, "old")
    clock.now += 100.0
    await _hold(m, "fresh")
    assert await m.reap() == ["old"]


async def test_a_reap_that_raises_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    clock = StopsAfter(1)
    m, _ = manager(FakeResource("a"), clock=clock)

    async def boom() -> None:
        raise RuntimeError("sweep blew up")

    m.before_reap = boom
    with caplog.at_level("ERROR", logger="airlie.manager.core"), pytest.raises(asyncio.CancelledError):
        await m.run(every_s=30)
    assert [r.getMessage() for r in caplog.records] == ["resource manager: reap failed"]


async def test_a_hold_on_a_stopped_resource_found_running_adopts_it_without_a_start() -> None:
    a = FakeResource("a")
    m, clock = manager(a)
    a.up = True  # started out of band (`airlie engine up`, another process) after serve's adopt
    clock.now = 7.0
    async with m.hold("a", "chat:1"):
        assert state(m, "a").state == "ready" and state(m, "a").since == 7.0
    assert a.starts == 0


async def test_a_hold_on_a_failed_resource_found_running_adopts_it_without_a_start() -> None:
    a = FakeResource("a")
    a.fail = "already running"
    m, _ = manager(a)
    with pytest.raises(EngineError):
        await _hold(m, "a")
    a.up = True
    await _hold(m, "a")
    assert a.starts == 1 and state(m, "a").state == "ready"


async def test_a_resource_adopted_on_hold_counts_for_memory() -> None:
    a, b = FakeResource("a", 10, lifecycle="pinned"), FakeResource("b", 10)
    a.up, a.fail = True, "already running"  # a start would fail as EngineProcess.start does
    m, _ = manager(a, b, budget_gb=15, wait_timeout_s=5)
    await _hold(m, "a")
    with pytest.raises(EngineError, match="gave up waiting"):
        await _hold(m, "b")
    assert b.starts == 0


async def test_a_wait_with_no_outside_holder_names_the_blocking_resources() -> None:
    a, b = FakeResource("a", 10, lifecycle="pinned"), FakeResource("b", 10)
    a.gate = asyncio.Event()
    m, _ = manager(a, b, budget_gb=15, wait_timeout_s=5)
    starting = asyncio.create_task(m.start_pinned())
    await settle()
    w = asyncio.create_task(_hold(m, "b"))
    await settle()
    assert state(m, "b").detail == "waiting for memory: a starting"
    a.gate.set()
    await starting
    with pytest.raises(EngineError):
        await w


async def test_a_wait_names_each_blocking_resource_joined_by_comma_space() -> None:
    a, b, c = (
        FakeResource("a", 10, lifecycle="pinned"),
        FakeResource("b", 10, lifecycle="pinned"),
        FakeResource("c", 10),
    )
    a.up = b.up = True
    m, _ = manager(a, b, c, budget_gb=25, wait_timeout_s=5)
    await m.adopt()
    w = asyncio.create_task(_hold(m, "c"))
    await settle()
    assert state(m, "c").detail == "waiting for memory: a ready, b ready"
    with pytest.raises(EngineError):
        await w


async def test_the_internal_start_owner_is_not_named_as_a_blocker() -> None:
    a, b = FakeResource("a", 10, lifecycle="pinned"), FakeResource("b", 10)
    m, _ = manager(a, b, budget_gb=15, wait_timeout_s=5)
    release = asyncio.Event()

    async def held_by_start() -> None:
        async with m.hold("a", "start"):
            await release.wait()

    h = asyncio.create_task(held_by_start())
    await settle()
    w = asyncio.create_task(_hold(m, "b"))
    await settle()
    assert state(m, "b").detail == "waiting for memory: a ready"
    release.set()
    await h
    with pytest.raises(EngineError):
        await w
