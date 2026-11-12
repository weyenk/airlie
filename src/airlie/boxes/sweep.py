"""Leftover VM disks: an image build that crashed leaves `airlie-env-*-building-*` forever, and a chat deleted
while serve was down leaves its `airlie-chat-*` VM. `leftovers` finds them (the doctor row reports them, and
never deletes); `sweep` deletes them (serve's start only). Both leave anything younger than the grace period
alone, and anything a build of this process owns."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from airlie.boxes.protocol import Box

if TYPE_CHECKING:
    from airlie.chat.threads import ThreadStore

log = logging.getLogger(__name__)

GRACE_S = 3600.0
BUILDING = re.compile(r"airlie-env-.+-building-[0-9a-f]+")
CHAT = re.compile(r"airlie-chat-([0-9a-f]{12})")


@dataclass(frozen=True)
class Vm:
    name: str
    size_gb: float
    running: bool
    accessed: datetime | None
    """When `tart list` says the VM was last used; None when it does not say (never swept then)."""


def parse_vms(raw: str) -> list[Vm] | None:
    """`tart list --format json` as Vms; None when it is not that."""
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    vms: list[Vm] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("Source") != "local" or not isinstance(row.get("Name"), str):
            continue
        accessed: datetime | None = None
        with contextlib.suppress(KeyError, ValueError):
            accessed = datetime.fromisoformat(str(row["Accessed"]).replace("Z", "+00:00"))
        vms.append(Vm(row["Name"], float(row.get("Size") or 0), bool(row.get("Running")), accessed))
    return vms


def tart_vms() -> list[Vm] | None:
    """The local VMs `tart list` shows; None when tart is missing or cannot be asked."""
    exe = shutil.which("tart")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "list", "--format", "json"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_vms(out.stdout) if out.returncode == 0 else None


def leftovers(
    vms: list[Vm],
    *,
    now: datetime,
    live_builds: Collection[str] = (),
    chat_slugs: Collection[str] | None = None,
    grace_s: float = GRACE_S,
) -> list[Vm]:
    """The VMs to sweep: a building disk no live build owns, and (when `chat_slugs`, the slugs of the stored
    repository chats, is given) a chat VM whose chat is gone; each unused for `grace_s`."""
    found: list[Vm] = []
    for vm in vms:
        if vm.accessed is None or (now - vm.accessed).total_seconds() < grace_s:
            continue
        if BUILDING.fullmatch(vm.name):
            if vm.name not in live_builds:
                found.append(vm)
        elif (m := CHAT.fullmatch(vm.name)) and chat_slugs is not None and m.group(1) not in chat_slugs:
            found.append(vm)
    return found


async def sweep(
    list_vms: Callable[[], list[Vm] | None],
    box_for: Callable[[str], Box],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    live_builds: Callable[[], Collection[str]] = lambda: (),
    chat_slugs: Callable[[], Collection[str]],
    grace_s: float = GRACE_S,
) -> list[str]:
    """Delete (stopping first when it runs) every leftover; a VM that will not go is logged and left. The
    names deleted."""
    vms = list_vms()
    if vms is None:
        return []
    gone: list[str] = []
    for vm in leftovers(vms, now=now(), live_builds=live_builds(), chat_slugs=chat_slugs(), grace_s=grace_s):
        try:
            await box_for(vm.name).delete()
        except Exception:
            log.warning("could not delete the leftover VM %s", vm.name, exc_info=True)
        else:
            gone.append(vm.name)
            log.info("deleted the leftover VM %s (%.0f GB)", vm.name, vm.size_gb)
    return gone


def serve_sweep(
    threads: ThreadStore,
    box_for: Callable[[str], Box],
    list_vms: Callable[[], list[Vm] | None] = tart_vms,
) -> Callable[[], Awaitable[list[str]]]:
    """Serve's start step: `sweep` against the stored repository chats and this process's builds."""
    from airlie.boxes.images import LIVE_BUILDS
    from airlie.chat.repo_session import slug_of

    async def sweep_leftovers() -> list[str]:
        slugs = {slug_of(t.id) for t in threads.repository_threads()}
        return await sweep(list_vms, box_for, live_builds=lambda: LIVE_BUILDS, chat_slugs=lambda: slugs)

    return sweep_leftovers
