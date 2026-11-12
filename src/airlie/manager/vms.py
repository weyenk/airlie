"""A repository chat's VM as a manager resource: created and booted on the first hold, stopped when idle (the
disk stays), deleted with the chat."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from airlie.boxes.protocol import Box, BoxUnavailable, Network
from airlie.config.schema import BoxesSpec
from airlie.engine.types import EngineError
from airlie.manager.core import Report


class VMResource:
    kind: Literal["engine", "vm"] = "vm"

    def __init__(
        self,
        session_id: str,
        repo: str,
        box: Box,
        spec: BoxesSpec,
        *,
        image: Callable[[], Awaitable[str]],
        network: Callable[[], Awaitable[Network]],
    ) -> None:
        self.name = f"vm:{session_id}"
        self.display = f"{repo} VM"
        self.lifecycle: Literal["pinned", "on_demand"] = "on_demand"
        self.idle_unload_s = spec.idle_stop_s
        self.footprint_gb = spec.memory_gb
        self.box = box
        self.spec = spec
        self._image = image
        self._network = network

    async def running(self) -> bool:
        return await self.box.running()

    async def start(self, report: Report) -> None:
        spec = self.spec
        try:
            if not await self.box.exists():
                report("starting", "building image", None)
                image = await self._image()
                # `tart clone` pulls an image it does not have yet, with no progress to show: the doctor's
                # tart row says to pull the default image once (`tart pull`)
                report("starting", f"cloning {image} (its first use downloads it)", None)
                await self.box.create(image, cpus=spec.cpus, memory_gb=spec.memory_gb, disk_gb=spec.disk_gb)
            report("starting", "starting VM", None)
            await self.box.boot(network=await self._network(), timeout_s=spec.boot_timeout_s)
        except BoxUnavailable as e:
            raise EngineError(str(e)) from e

    async def stop(self) -> None:
        await self.box.stop()

    async def delete(self) -> None:
        await self.box.delete()
