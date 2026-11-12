"""The generic resource manager.

A caller `hold`s a resource for the duration of its work: the manager starts it once however many callers ask
at the same time, makes room by stopping idle on-demand resources (least recently used first), waits with a
reason when everything that could go is held, and `reap`s on-demand resources left idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal, Protocol

from airlie.engine.types import EngineError
from airlie.models_download import DownloadProgress
from airlie.serve.bridge.clock import Clock

log = logging.getLogger(__name__)

State = Literal["stopped", "downloading", "starting", "ready", "waiting", "stopping", "failed"]
Report = Callable[[State, str, DownloadProgress | None], None]
WAIT_STEP_S = 1.0  # a waiter re-checks at least this often, by the clock (a release wakes it sooner)
MEMORY_IN_USE = ("ready", "starting", "downloading", "stopping")
START_OWNER = "start"  # the manager's own hold while it starts the pinned resources: no one to wait on


@dataclass(frozen=True)
class Status:
    name: str
    kind: Literal["engine", "vm"]
    state: State
    detail: str = ""
    progress: DownloadProgress | None = None
    holders: tuple[str, ...] = ()
    since: float = 0.0
    lifecycle: Literal["pinned", "on_demand"] = "pinned"


class Resource(Protocol):
    name: str
    kind: Literal["engine", "vm"]
    lifecycle: Literal["pinned", "on_demand"]
    idle_unload_s: float
    footprint_gb: float

    async def running(self) -> bool: ...

    async def start(self, report: Report) -> None: ...

    async def stop(self) -> None: ...


@dataclass
class _Slot:
    resource: Resource
    state: State = "stopped"
    detail: str = ""
    progress: DownloadProgress | None = None
    holders: list[str] = field(default_factory=list)
    since: float = 0.0
    last_used: float = 0.0
    stop_failed: bool = False  # its stop raised: the process may be alive, so it still counts for memory
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Manager:
    def __init__(
        self,
        resources: Sequence[Resource],
        *,
        budget_gb: float,
        clock: Clock,
        on_change: Callable[[Status], None] = lambda s: None,
        wait_timeout_s: float = 1800.0,
    ) -> None:
        self._slots = {r.name: _Slot(r) for r in resources}
        self._budget_gb = budget_gb
        self._clock = clock
        self._on_change = on_change
        self._wait_timeout_s = wait_timeout_s
        self._changed = asyncio.Event()
        self.before_reap: Callable[[], Awaitable[None]] | None = None  # e.g. the lease expiry sweep

    @property
    def clock(self) -> Clock:
        return self._clock

    # ---- the registry ----

    def add(self, resource: Resource) -> None:
        """Register a resource after construction (a VM per chat session)."""
        if resource.name in self._slots:
            raise ValueError(f"{resource.name}: already registered")
        self._slots[resource.name] = _Slot(resource)
        self._notify()

    def remove(self, name: str) -> None:
        """Forget a resource nobody holds or is starting or stopping; the caller deletes what it owns."""
        slot = self._slot(name)
        if slot.holders or slot.lock.locked():
            raise EngineError(f"{name}: in use")
        if slot.stop_failed:
            raise EngineError(f"{name}: {slot.detail}")
        if slot.state in MEMORY_IN_USE:
            raise EngineError(f"{name}: running (discard it to stop it first)")
        del self._slots[name]
        self._notify()

    async def discard(self, name: str) -> Resource:
        """Stop a resource nobody holds if it is running, forget it, and return it so the caller can delete
        what it owns (a chat's VM disk)."""
        slot = self._slot(name)
        if slot.holders:
            raise EngineError(f"{name}: in use")
        async with slot.lock:
            if slot.holders:
                raise EngineError(f"{name}: in use")
            if slot.state in MEMORY_IN_USE or slot.stop_failed or await slot.resource.running():
                self._set(slot, "stopping")
                try:
                    await slot.resource.stop()
                except Exception as e:
                    slot.stop_failed = True
                    self._set(slot, "failed", f"stop failed: {e}")
                    raise EngineError(f"{name}: stop failed: {e}") from e
                slot.stop_failed = False
                self._set(slot, "stopped")
            del self._slots[name]
        self._notify()
        return slot.resource

    def _slot(self, name: str) -> _Slot:
        try:
            return self._slots[name]
        except KeyError:
            raise EngineError(f"{name} is not a resource") from None

    # ---- state ----

    def _status(self, slot: _Slot) -> Status:
        r = slot.resource
        return Status(
            r.name,
            r.kind,
            slot.state,
            slot.detail,
            slot.progress,
            tuple(slot.holders),
            slot.since,
            r.lifecycle,
        )

    def watch(self, callback: Callable[[Status], None]) -> None:
        """Also call `callback` on every status change."""
        previous = self._on_change

        def both(status: Status) -> None:
            previous(status)
            callback(status)

        self._on_change = both

    def status(self) -> list[Status]:
        return [self._status(s) for s in self._slots.values()]

    def _set(
        self, slot: _Slot, state: State, detail: str = "", progress: DownloadProgress | None = None
    ) -> None:
        slot.state, slot.detail, slot.progress = state, detail, progress
        slot.since = self._clock.monotonic()
        self._on_change(self._status(slot))

    def _notify(self) -> None:
        old, self._changed = self._changed, asyncio.Event()
        old.set()

    def touch(self, name: str) -> None:
        self._slots[name].last_used = self._clock.monotonic()

    def _using_memory(self, excluding: str) -> list[_Slot]:
        return [
            s
            for n, s in list(self._slots.items())
            if n != excluding and (s.state in MEMORY_IN_USE or s.stop_failed)
        ]

    def _used_gb(self, excluding: str) -> float:
        return sum(s.resource.footprint_gb for s in self._using_memory(excluding))

    def _blockers(self, excluding: str) -> list[str]:
        """Who holds the memory: the outside holders, else the resources using it and what they are doing."""
        others = self._using_memory(excluding)
        holders = [o for s in others for o in s.holders if o != START_OWNER]
        return holders or [f"{s.resource.name} {s.state}" for s in others]

    def _victim(self, excluding: str) -> _Slot | None:
        idle = [
            s
            for n, s in list(self._slots.items())
            if n != excluding
            and s.state == "ready"
            and not s.holders
            and not s.lock.locked()
            and s.resource.lifecycle == "on_demand"
        ]
        return min(idle, key=lambda s: s.last_used, default=None)

    # ---- the verbs ----

    async def _stop(self, slot: _Slot) -> None:
        """Stop under the slot's lock, so no start of the same resource overlaps it."""
        async with slot.lock:
            self._set(slot, "stopping")
            try:
                await slot.resource.stop()
            except Exception as e:
                slot.stop_failed = True
                self._set(slot, "failed", f"stop failed: {e}")
            else:
                slot.stop_failed = False
                self._set(slot, "stopped")
        self._notify()

    async def stop(self, name: str) -> None:
        """Stop a resource nobody holds, keeping it registered (serve's shutdown stops its VMs this way)."""
        slot = self._slot(name)
        if slot.holders:
            raise EngineError(f"{name}: in use")
        await self._stop(slot)

    async def _forget_the_dead(self) -> None:
        """A resource whose stop failed counts for memory until it is seen not running."""
        for slot in list(self._slots.values()):
            if slot.stop_failed and not slot.lock.locked() and not await slot.resource.running():
                slot.stop_failed = False
                self._set(slot, "stopped")

    async def _wait_for_change(self, deadline: float, name: str) -> None:
        remaining = deadline - self._clock.monotonic()
        if remaining <= 0:
            raise EngineError(f"{name}: gave up waiting for memory after {self._wait_timeout_s:.0f}s")
        woke = asyncio.ensure_future(self._changed.wait())
        tick = asyncio.ensure_future(self._clock.sleep(min(remaining, WAIT_STEP_S)))
        try:
            await asyncio.wait({woke, tick}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            woke.cancel()
            tick.cancel()

    async def _make_room(self, slot: _Slot) -> None:
        name, need = slot.resource.name, slot.resource.footprint_gb
        deadline = self._clock.monotonic() + self._wait_timeout_s
        before = slot.state
        try:
            while True:
                await self._forget_the_dead()
                if self._used_gb(name) + need <= self._budget_gb:
                    break
                victim = self._victim(name)
                if victim is not None:
                    await self._stop(victim)
                    continue
                reason = "waiting for memory: " + ", ".join(self._blockers(name))
                if slot.state != "waiting" or slot.detail != reason:
                    self._set(slot, "waiting", reason)
                await self._wait_for_change(deadline, name)
        except BaseException:
            self._set(slot, before)
            raise

    async def _start(self, slot: _Slot) -> None:
        def report(state: State, detail: str, progress: DownloadProgress | None) -> None:
            self._set(slot, state, detail, progress)

        self._set(slot, "starting")
        try:
            await slot.resource.start(report)
        except asyncio.CancelledError:  # a Stop mid-start: not a failure, unless what it started lives on
            alive = True  # cannot say (the check failed, or was cancelled too): counted until seen gone
            with contextlib.suppress(BaseException):
                alive = await slot.resource.running()
            if alive:
                slot.stop_failed = True  # counts for memory until it is seen not running
                self._set(slot, "failed", "start cancelled with it still running")
            else:
                self._set(slot, "stopped")
            self._notify()
            raise
        except BaseException as e:
            self._set(slot, "failed", str(e) or type(e).__name__)
            self._notify()
            raise
        self._set(slot, "ready")
        slot.last_used = self._clock.monotonic()

    @asynccontextmanager
    async def hold(self, name: str, owner: str) -> AsyncIterator[None]:
        slot = self._slot(name)
        async with slot.lock:
            if self._slots.get(name) is not slot:
                raise EngineError(f"{name} is not a resource")  # discarded while this hold waited its turn
            if slot.stop_failed:
                if await slot.resource.running():
                    raise EngineError(f"{name}: {slot.detail}")
                slot.stop_failed = False
            if slot.state == "ready" and not await slot.resource.running():
                self._set(slot, "failed", "exited")
            if slot.state != "ready":
                if await slot.resource.running():
                    self._set(slot, "ready")  # started out of band since (`airlie engine up`, a local start)
                else:
                    await self._make_room(slot)
                    await self._start(slot)
            slot.holders.append(owner)
            self._on_change(self._status(slot))
        try:
            yield
        finally:
            slot.holders.remove(owner)
            slot.last_used = self._clock.monotonic()
            self._on_change(self._status(slot))
            self._notify()

    async def adopt(self) -> None:
        for slot in list(self._slots.values()):
            if self._slots.get(slot.resource.name) is slot and await slot.resource.running():
                self._set(slot, "ready")
                slot.last_used = self._clock.monotonic()

    async def start_pinned(self) -> None:
        for name, slot in list(self._slots.items()):
            if slot.resource.lifecycle == "pinned" and slot.state != "ready":
                try:
                    async with self.hold(name, START_OWNER):
                        pass
                except EngineError:
                    pass  # its status says why; the others still start

    async def reap(self) -> list[str]:
        if self.before_reap is not None:
            await self.before_reap()
        stopped: list[str] = []
        for name, slot in list(self._slots.items()):
            r = slot.resource
            if self._slots.get(name) is not slot:
                continue  # removed while an earlier stop was awaited
            if r.lifecycle != "on_demand" or slot.state != "ready" or slot.holders or slot.lock.locked():
                continue
            if self._clock.monotonic() - slot.last_used < r.idle_unload_s:
                continue
            await self._stop(slot)
            stopped.append(name)
        return stopped

    async def run(self, every_s: float) -> None:
        while True:
            await self._clock.sleep(every_s)
            try:
                await self.reap()
            except Exception:
                log.exception("resource manager: reap failed")  # the next sweep still runs
