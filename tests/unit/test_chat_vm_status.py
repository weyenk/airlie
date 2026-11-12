"""A repository chat's VM start is shown in the stream as the engine wait is: the manager's status row for
`vm:<thread>` goes out as `data-engine` parts ("starting VM", "waiting for memory: ...") while the turn waits
for it. A real Manager over a VMResource on a FakeBox whose boot waits for the test; a scripted chat
engine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from airlie.boxes.protocol import Network
from airlie.chat.hooks import TurnContext
from airlie.chat.threads import StoredMessage
from airlie.chat.turn import TurnRequest, run_turn
from airlie.config.schema import BoxesSpec, RoleSpec
from airlie.manager.client import InProcessHolder
from airlie.manager.core import Manager
from airlie.manager.vms import VMResource
from airlie.serve.bridge.clock import FakeClock
from tests.support.box import FakeBox
from tests.unit.test_chat_turn import USER, Rig, Step, collect, parse, reply, words
from tests.unit.test_manager import FakeResource


@pytest.fixture
def role() -> RoleSpec:
    return RoleSpec.model_validate({"prompt": "p.md", "model": "m"})


class GatedBoot(FakeBox):
    """A boot that waits until the test lets it answer."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.go = asyncio.Event()

    async def boot(self, *, network: Network, timeout_s: float) -> None:
        await self.go.wait()
        await super().boot(network=network, timeout_s=timeout_s)


async def image() -> str:
    return "ghcr.io/cirruslabs/ubuntu:latest"


async def network() -> Network:
    return Network("off")


def vm_rig(tmp_path: Path, role: RoleSpec, *, budget_gb: float = 100.0) -> tuple[Rig, Manager, GatedBoot]:
    box = GatedBoot("airlie-chat-x")
    vm = VMResource("t1", "o/r", box, BoxesSpec(memory_gb=8), image=image, network=network)
    manager = Manager(
        [FakeResource("chat-engine", footprint_gb=1), FakeResource("other", footprint_gb=8)],
        budget_gb=budget_gb,
        clock=FakeClock(),
    )
    manager.add(vm)
    rig = Rig(tmp_path, role, [Step(reply("hi"), words("hi"))])
    rig.holder = InProcessHolder(manager)  # type: ignore[assignment]
    rig.status = manager.status  # type: ignore[method-assign]
    return rig, manager, box


def vm_rows(chunks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (c["data"]["state"], c["data"]["detail"])
        for c in chunks
        if c["type"] == "data-engine" and c["data"]["engine"] == "vm:t1"
    ]


def turn_holding_the_vm(rig: Rig, manager: Manager) -> Any:
    user = StoredMessage("u-1", "user", [{"type": "text", "text": "hello"}], 1.0)
    ctx = TurnContext("t1", "u1", "repository", USER, system_prompt="SYSTEM")
    return run_turn(
        TurnRequest(rig.thread, user, ctx),
        engine=rig.engine,
        engine_name="chat-engine",
        holder=rig.holder,
        registry=rig.registry,
        todos=rig.todos,
        store=rig.store,
        hooks=rig.hooks,
        role=rig.role,
        max_steps=rig.max_steps,
        clock=rig.clock,
        status=rig.status,
        around=lambda: manager.hold("vm:t1", "chat:t1"),
        around_status="vm:t1",
    )


async def test_the_vm_start_is_relayed_in_the_stream_while_the_turn_waits(
    tmp_path: Path, role: RoleSpec
) -> None:
    rig, manager, box = vm_rig(tmp_path, role)

    async def boot_once_shown() -> None:
        while not any(s.name == "vm:t1" and s.detail == "starting VM" for s in manager.status()):
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        box.go.set()

    booter = asyncio.create_task(boot_once_shown())
    chunks = parse(await asyncio.wait_for(collect(turn_holding_the_vm(rig, manager)), 5))
    await booter

    assert ("starting", "starting VM") in vm_rows(chunks)
    assert [c["delta"] for c in chunks if c["type"] == "text-delta"] == ["hi"]


async def test_a_vm_waiting_for_memory_says_who_holds_it_in_the_stream(
    tmp_path: Path, role: RoleSpec
) -> None:
    rig, manager, box = vm_rig(tmp_path, role, budget_gb=12.0)
    box.go.set()
    release = asyncio.Event()

    async def hold_other() -> None:
        async with manager.hold("other", "story:1"):
            await release.wait()

    async def let_go_once_waiting() -> None:
        while not any(s.name == "vm:t1" and s.state == "waiting" for s in manager.status()):
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()

    blocker = asyncio.create_task(hold_other())
    await asyncio.sleep(0)
    releaser = asyncio.create_task(let_go_once_waiting())
    chunks = parse(await asyncio.wait_for(collect(turn_holding_the_vm(rig, manager)), 5))
    await asyncio.gather(blocker, releaser)

    assert ("waiting", "waiting for memory: story:1") in vm_rows(chunks)
