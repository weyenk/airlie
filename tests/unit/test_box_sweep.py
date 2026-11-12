"""The leftover-VM sweep over fake VM lists and FakeBox: nothing here touches tart."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from airlie.boxes.images import LIVE_BUILDS
from airlie.boxes.sweep import Vm, leftovers, parse_vms, sweep
from tests.support.box import FakeBox

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(hours=3)
BUILD = "airlie-env-abc123-building-deadbeef"
CHAT = "airlie-chat-0123456789ab"


def vm(name: str, accessed: datetime | None = OLD, running: bool = False, size: float = 20) -> Vm:
    return Vm(name, size, running, accessed)


def test_parse_keeps_local_vms_with_size_and_access_time() -> None:
    raw = json.dumps(
        [
            {
                "Source": "local",
                "Name": BUILD,
                "Size": 7,
                "Running": True,
                "Accessed": "2026-11-12T08:00:00Z",
            },
            {"Source": "OCI", "Name": "ghcr.io/x/y:latest", "Size": 5, "Running": False},
        ]
    )
    assert parse_vms(raw) == [Vm(BUILD, 7.0, True, datetime(2026, 11, 12, 8, tzinfo=UTC))]
    assert parse_vms("nope") is None


def test_an_old_building_disk_nobody_owns_is_a_leftover_but_not_a_young_or_owned_one() -> None:
    young = vm("airlie-env-k-building-00000001", NOW - timedelta(minutes=10))
    owned = vm("airlie-env-k-building-00000002")
    old = vm(BUILD)
    got = leftovers([young, owned, old, vm("airlie-env-k")], now=NOW, live_builds={owned.name})
    assert got == [old]


def test_a_chat_vm_is_a_leftover_only_when_its_thread_is_gone() -> None:
    assert leftovers([vm(CHAT)], now=NOW, chat_slugs={"0123456789ab"}) == []
    assert leftovers([vm(CHAT)], now=NOW, chat_slugs=set()) == [vm(CHAT)]
    assert leftovers([vm(CHAT)], now=NOW, chat_slugs=None) == []  # threads unknown: never guess


def test_sweep_deletes_leftovers_through_the_box_and_a_live_build_survives() -> None:
    boxes: dict[str, FakeBox] = {}

    def box_for(name: str) -> FakeBox:
        return boxes.setdefault(name, FakeBox(name, state="running"))

    live = "airlie-env-k-building-0000000a"
    LIVE_BUILDS.add(live)
    try:
        vms = [vm(BUILD, running=True), vm(CHAT), vm(live)]
        gone = asyncio.run(
            sweep(
                lambda: vms,
                box_for,
                now=lambda: NOW,
                live_builds=lambda: LIVE_BUILDS,
                chat_slugs=lambda: set(),
            )
        )
    finally:
        LIVE_BUILDS.discard(live)
    assert gone == [BUILD, CHAT]
    assert boxes[BUILD].deleted and live not in boxes


def test_sweep_with_tart_unavailable_deletes_nothing() -> None:
    assert asyncio.run(sweep(lambda: None, lambda n: FakeBox(n), chat_slugs=lambda: set())) == []


def test_serve_sweep_keeps_the_chat_vm_of_a_stored_repository_thread() -> None:
    from airlie.boxes.sweep import serve_sweep
    from airlie.chat.repo_session import slug_of

    class Threads:
        def repository_threads(self):  # type: ignore[no-untyped-def]
            return [type("T", (), {"id": "kept"})()]

    kept, orphan = f"airlie-chat-{slug_of('kept')}", "airlie-chat-ffffffffffff"
    boxes = {n: FakeBox(n, state="stopped") for n in (kept, orphan)}
    run = serve_sweep(Threads(), boxes.__getitem__, lambda: [vm(kept), vm(orphan)])  # type: ignore[arg-type]
    assert asyncio.run(run()) == [orphan]
    assert boxes[orphan].deleted and not boxes[kept].deleted


def test_a_failing_delete_is_left_and_the_rest_still_go() -> None:
    class Stuck(FakeBox):
        async def delete(self) -> None:
            raise RuntimeError("busy")

    boxes = {BUILD: Stuck(BUILD), CHAT: FakeBox(CHAT, state="stopped")}
    gone = asyncio.run(
        sweep(lambda: [vm(BUILD), vm(CHAT)], boxes.__getitem__, now=lambda: NOW, chat_slugs=lambda: set())
    )
    assert gone == [CHAT]
