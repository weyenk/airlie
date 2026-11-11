"""Leases: a client that needs an engine up for a while (a Mac app, the studio) holds it through the API.

A lease is a hold owned by `lease:{id}`, kept open by a task until it is deleted or its ttl passes with no
renewal; the manager's reap sweep (`Manager.before_reap`) expires them on the manager's clock.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from airlie.manager.core import Manager


class UnknownLease(KeyError):
    pass


@dataclass
class _Lease:
    engine: str
    task: asyncio.Task[None]
    release: asyncio.Event
    deadline: float = 0.0


class Leases:
    def __init__(self, manager: Manager, *, ttl_s: float) -> None:
        self._manager = manager
        self._ttl_s = ttl_s
        self._clock = manager.clock
        self._leases: dict[str, _Lease] = {}
        manager.before_reap = self.expire

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    async def acquire(self, engine: str) -> str:
        """Hold `engine` (starting it if need be) for a new lease; raises what `Manager.hold` raises."""
        lease_id = uuid.uuid4().hex
        release = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def keep() -> None:
            try:
                async with self._manager.hold(engine, f"lease:{lease_id}"):
                    ready.set_result(None)
                    await release.wait()
            except BaseException as e:
                if not ready.done():
                    ready.set_exception(e)
                    ready.exception()  # retrieved here: the caller below re-raises it
                if isinstance(e, asyncio.CancelledError):
                    raise
            finally:
                self._leases.pop(lease_id, None)

        task = asyncio.ensure_future(keep())
        try:
            await ready
        except BaseException:
            task.cancel()
            raise
        self._leases[lease_id] = _Lease(engine, task, release, self._clock.monotonic() + self._ttl_s)
        return lease_id

    def _get(self, engine: str, lease_id: str) -> _Lease:
        lease = self._leases.get(lease_id)
        if lease is None or lease.engine != engine:
            raise UnknownLease(lease_id)
        return lease

    def renew(self, engine: str, lease_id: str) -> None:
        lease = self._get(engine, lease_id)
        lease.deadline = self._clock.monotonic() + self._ttl_s
        self._manager.touch(engine)

    async def release(self, engine: str, lease_id: str) -> None:
        lease = self._get(engine, lease_id)
        lease.release.set()
        await lease.task

    async def expire(self) -> None:
        now = self._clock.monotonic()
        for lease in list(self._leases.values()):
            if lease.deadline <= now:
                lease.release.set()
                await lease.task

    async def close(self) -> None:
        """Release every lease (shutdown): the engines stay running."""
        for lease in list(self._leases.values()):
            lease.release.set()
            await lease.task
