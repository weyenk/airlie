"""scripts/searxng_vm.py: SearXNG in a Tart VM. Tart is faked; the loopback forward uses real sockets."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "searxng_vm.py"
spec = importlib.util.spec_from_file_location("searxng_vm", SCRIPT)
assert spec and spec.loader
sv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sv)


class FakeTart:
    """tart, as far as the script uses it: a list of VMs, `ip`, `exec` and the rest recorded."""

    def __init__(self, vms: list[dict[str, str]] | None = None, installed: str = "") -> None:
        self.vms = vms if vms is not None else []
        self.installed = installed
        self.calls: list[tuple[str, ...]] = []
        self.stdin: list[str | None] = []

    def __call__(
        self, *args: str, stdin: str | None = None, stream: bool = False, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.stdin.append(stdin)
        out = ""
        code = 0
        match args:
            case ("list", *_):
                out = json.dumps(self.vms)
            case ("clone", _, name):
                self.vms.append({"Source": "local", "Name": name, "State": "stopped"})
            case ("ip", *_):
                out = "192.168.64.7\n"
            case ("exec", _, "cat", _):
                out, code = (self.installed + "\n", 0) if self.installed else ("", 1)
            case ("exec", _, "curl", *_):
                pass
            case ("stop", name):
                for vm in self.vms:
                    if vm["Name"] == name:
                        vm["State"] = "stopped"
            case ("delete", name):
                self.vms[:] = [v for v in self.vms if v["Name"] != name]
        return subprocess.CompletedProcess(["tart", *args], code, out, "")

    def ran(self, *prefix: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[: len(prefix)] == prefix]


def local_vm(state: str) -> dict[str, str]:
    return {"Source": "local", "Name": sv.VM, "State": state}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """The script with its outside world faked: tart, the VM start, the forward and the search check."""
    fake = FakeTart()
    seen: dict[str, Any] = {"spawned": 0, "forward": None, "stopped_forward": 0}

    def spawn() -> None:
        seen["spawned"] += 1
        for vm in fake.vms:
            vm["State"] = "running"

    monkeypatch.setattr(sv, "tart", fake)
    monkeypatch.setattr(sv, "spawn_vm", spawn)
    monkeypatch.setattr(sv, "STATE", tmp_path / "state")
    monkeypatch.setattr(sv, "start_forward", lambda port, ip: seen.__setitem__("forward", (port, ip)) or 4242)
    monkeypatch.setattr(
        sv, "stop_forward", lambda: seen.__setitem__("stopped_forward", seen["stopped_forward"] + 1) or True
    )
    monkeypatch.setattr(sv, "json_search_answers", lambda port: True)
    monkeypatch.setattr(sv, "wait_for", lambda what, ready, seconds, every=2.0: None)
    return {"tart": fake, "seen": seen}


def test_up_on_a_clean_machine_makes_the_vm_starts_it_installs_searxng_and_forwards_it(
    world: dict[str, Any],
) -> None:
    tart, seen = world["tart"], world["seen"]
    assert sv.cmd_up(8888) == 0
    assert tart.ran("clone", sv.IMAGE, sv.VM) and seen["spawned"] == 1
    [install] = tart.ran("exec", "-i", sv.VM, "bash", "-s", "--", sv.pinned_commit())
    assert install  # the provisioning script went in on stdin
    assert (
        "searxng" in tart.stdin[tart.calls.index(install)]
        and "systemctl" in tart.stdin[tart.calls.index(install)]
    )
    assert tart.ran("exec", sv.VM, "sudo", "systemctl", "enable", "--now", "searxng")
    assert seen["forward"] == (8888, "192.168.64.7")


def test_up_with_the_vm_running_and_searxng_at_the_pinned_commit_installs_nothing(
    world: dict[str, Any],
) -> None:
    tart = world["tart"]
    tart.vms.append(local_vm("running"))
    tart.installed = sv.pinned_commit()
    assert sv.cmd_up(9999) == 0
    assert not tart.ran("clone") and world["seen"]["spawned"] == 0
    assert not tart.ran("exec", "-i")
    assert world["seen"]["forward"] == (9999, "192.168.64.7")


def test_up_reinstalls_when_the_pinned_commit_changed(world: dict[str, Any]) -> None:
    tart = world["tart"]
    tart.vms.append(local_vm("running"))
    tart.installed = "0" * 40
    sv.cmd_up(8888)
    assert tart.ran("exec", "-i", sv.VM, "bash", "-s", "--", sv.pinned_commit())


def test_up_starts_a_stopped_vm_without_cloning_it_again(world: dict[str, Any]) -> None:
    tart = world["tart"]
    tart.vms.append(local_vm("stopped"))
    tart.installed = sv.pinned_commit()
    sv.cmd_up(8888)
    assert not tart.ran("clone") and world["seen"]["spawned"] == 1


def test_down_stops_the_forward_and_the_vm_but_keeps_the_disk(world: dict[str, Any]) -> None:
    tart = world["tart"]
    tart.vms.append(local_vm("running"))
    assert sv.cmd_down() == 0
    assert world["seen"]["stopped_forward"] == 1 and tart.ran("stop", sv.VM) and not tart.ran("delete")


def test_destroy_deletes_the_vm_and_a_destroy_with_no_vm_is_fine(world: dict[str, Any]) -> None:
    tart = world["tart"]
    tart.vms.append(local_vm("running"))
    assert sv.cmd_destroy() == 0 and tart.ran("delete", sv.VM) and tart.vms == []
    assert sv.cmd_destroy() == 0


def test_vm_state_ignores_the_oci_cache_and_other_vms(world: dict[str, Any]) -> None:
    tart = world["tart"]
    tart.vms[:] = [
        {"Source": "OCI", "Name": sv.VM, "State": "stopped"},
        {"Source": "local", "Name": "other", "State": "running"},
    ]
    assert sv.vm_state() is None
    tart.vms.append(local_vm("running"))
    assert sv.vm_state() == "running"


def test_without_tart_the_error_says_how_to_get_it(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*a: Any, **k: Any) -> None:
        raise FileNotFoundError("tart")

    monkeypatch.setattr(sv.subprocess, "run", missing)
    with pytest.raises(sv.VmError, match="tart is not installed"):
        sv.tart("list")
    assert sv.main(["status"]) == 1  # reported, not a traceback


def test_a_failing_tart_command_is_a_vm_error_with_its_last_line(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*a: Any, **k: Any) -> None:
        raise subprocess.CalledProcessError(1, a[0], "", "first line\nthe real reason")

    monkeypatch.setattr(sv.subprocess, "run", failing)
    with pytest.raises(sv.VmError, match="the real reason"):
        sv.tart("clone", "x", "y")


def test_the_pinned_commit_is_a_full_sha() -> None:
    assert len(sv.pinned_commit()) == 40 and set(sv.pinned_commit()) <= set("0123456789abcdef")


def test_the_provisioning_script_installs_that_commit_as_a_service_with_json_on() -> None:
    script = sv.PROVISION.read_text()
    assert 'COMMIT="${1:?' in script and "searxng.service" in script and "formats: [html, json]" in script
    assert (
        "limiter: false" in script and "secret_key" in script and "__SECRET__" not in script
    )  # made in the VM


async def test_the_forward_carries_bytes_both_ways_on_loopback_only() -> None:
    async def echo(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.write(b"echo:" + await r.read(100))
        await w.drain()
        w.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    listen = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    ready = asyncio.Event()
    task = asyncio.create_task(sv.forward(listen, "127.0.0.1", up_port, ready))
    await asyncio.wait_for(ready.wait(), 5)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listen)
        writer.write(b"hello")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), 5) == b"echo:hello"
        writer.close()
    finally:
        task.cancel()
        upstream.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_forward_to_nothing_closes_the_client_instead_of_hanging() -> None:
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    probe2 = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    listen = probe2.sockets[0].getsockname()[1]
    probe2.close()
    await probe2.wait_closed()
    ready = asyncio.Event()
    task = asyncio.create_task(sv.forward(listen, "127.0.0.1", dead, ready))
    await asyncio.wait_for(ready.wait(), 5)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listen)
        assert await asyncio.wait_for(reader.read(10), 5) == b""
        writer.close()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_the_forward_pid_file_is_cleaned_when_stale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sv, "STATE", tmp_path)
    sv.pid_file().write_text("999999")
    assert sv.forward_pid() is None and not sv.pid_file().exists()
