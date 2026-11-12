"""A VM as a manager resource: created and booted on a hold, counted in the budget, evicted and idle-stopped
like an engine, its disk kept until delete. FakeBox, FakeClock and a real Manager."""

from __future__ import annotations

import asyncio

import pytest

from airlie.boxes.protocol import Network
from airlie.config.schema import BoxesSpec
from airlie.engine.types import EngineError
from airlie.manager.core import Manager, Status
from airlie.manager.vms import VMResource
from airlie.serve.bridge.clock import FakeClock
from tests.support.box import FakeBox
from tests.unit.test_manager import FakeResource, settle


class Wiring:
    def __init__(self) -> None:
        self.images = 0

    async def image(self) -> str:
        self.images += 1
        return "airlie-env-abc"

    async def network(self) -> Network:
        return Network("off")


def vm(
    session: str = "s1", *, box: FakeBox | None = None, spec: BoxesSpec | None = None
) -> tuple[VMResource, FakeBox, Wiring]:
    box = box or FakeBox(f"chat-{session}")
    wiring = Wiring()
    r = VMResource(session, "acme/app", box, spec or BoxesSpec(), image=wiring.image, network=wiring.network)
    return r, box, wiring


def state(m: Manager, name: str) -> Status:
    return next(s for s in m.status() if s.name == name)


def manager(*resources, budget_gb: float = 100.0, clock: FakeClock | None = None):
    clock = clock or FakeClock()
    return Manager(resources, budget_gb=budget_gb, clock=clock), clock


async def test_the_first_hold_creates_and_boots_the_vm_and_a_second_hold_reuses_it() -> None:
    r, box, wiring = vm()
    m, _ = manager(r)
    async with m.hold(r.name, "chat:1"):
        pass
    async with m.hold(r.name, "chat:1"):
        pass
    assert len(box.boots) == 1 and len(box.created) == 1 and wiring.images == 1
    assert box.created[0] == ("airlie-env-abc", 4, 8.0, 40)
    assert box.boots == [Network("off")]


async def test_a_vm_is_named_and_shown_by_its_session_and_repository() -> None:
    r, _, _ = vm("s9")
    assert (r.name, r.display, r.kind, r.lifecycle) == ("vm:s9", "acme/app VM", "vm", "on_demand")


async def test_the_start_reports_preparing_while_the_disk_is_cloned() -> None:
    r, _, _ = vm()
    m, _ = manager(r)
    details: list[str] = []
    m.watch(lambda s: details.append(s.detail))
    async with m.hold(r.name, "o"):
        pass
    assert "preparing the environment" in details


async def test_footprint_is_the_configured_memory_and_counts_in_the_budget() -> None:
    engine = FakeResource("big", 14.0)
    r, _, _ = vm(spec=BoxesSpec(memory_gb=8.0))
    m, _ = manager(engine, r, budget_gb=20.0)
    assert r.footprint_gb == 8.0
    async with m.hold("big", "chat:1"):
        task = asyncio.create_task(m.hold(r.name, "chat:2").__aenter__())
        await settle()
        assert state(m, r.name).state == "waiting"
        assert state(m, r.name).detail == "waiting for memory: chat:1"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_an_idle_vm_is_evicted_for_an_engine_but_a_held_one_is_not() -> None:
    engine = FakeResource("big", 14.0)
    r, box, _ = vm()
    m, _ = manager(engine, r, budget_gb=20.0)
    async with m.hold(r.name, "chat:1"):
        task = asyncio.create_task(m.hold("big", "chat:2").__aenter__())
        await settle()
        assert state(m, "big").state == "waiting" and box.state == "running"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with m.hold("big", "chat:2"):
        assert box.state == "stopped" and state(m, r.name).state == "stopped"


async def test_an_idle_vm_stops_after_idle_stop_s_and_its_disk_is_kept() -> None:
    r, box, _ = vm(spec=BoxesSpec(idle_stop_s=300.0))
    m, clock = manager(r)
    async with m.hold(r.name, "o"):
        pass
    clock.now += 300.0
    assert await m.reap() == [r.name]
    assert box.state == "stopped" and box.deleted is False


async def test_the_next_hold_boots_it_again_without_cloning() -> None:
    r, box, wiring = vm()
    m, clock = manager(r)
    async with m.hold(r.name, "o"):
        pass
    clock.now += BoxesSpec().idle_stop_s
    await m.reap()
    async with m.hold(r.name, "o"):
        assert box.state == "running"
    assert len(box.boots) == 2 and len(box.created) == 1 and wiring.images == 1


async def test_a_boot_failure_is_a_failed_status_with_the_reason() -> None:
    box = FakeBox("chat-s1", fail_boot="no guest agent")
    r, _, _ = vm(box=box)
    m, _ = manager(r)
    with pytest.raises(EngineError, match="no guest agent"):
        async with m.hold(r.name, "o"):
            pass
    s = state(m, r.name)
    assert s.state == "failed" and s.detail == "no guest agent"


async def test_a_vm_that_exited_is_failed_and_the_next_hold_restarts_it() -> None:
    r, box, _ = vm()
    m, _ = manager(r)
    async with m.hold(r.name, "o"):
        pass
    box.state = "stopped"  # the guest powered off
    async with m.hold(r.name, "o"):
        assert box.state == "running"
    assert len(box.boots) == 2 and len(box.created) == 1


async def test_delete_removes_the_disk_after_removal_from_the_manager() -> None:
    r, box, _ = vm()
    m, clock = manager(r)
    async with m.hold(r.name, "o"):
        pass
    clock.now += BoxesSpec().idle_stop_s
    await m.reap()
    m.remove(r.name)
    await r.delete()
    assert box.deleted is True and r.name not in [s.name for s in m.status()]
