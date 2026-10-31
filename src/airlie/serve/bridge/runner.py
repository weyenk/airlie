"""Claim loop: short-poll the hub, run one job at a time on serve."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from airlie.config.schema import AirlieConfig, BridgeSpec
from airlie.serve.bridge.airlie_client import ServeClient
from airlie.serve.bridge.busy import busy_reason
from airlie.serve.bridge.clock import Clock, RealClock
from airlie.serve.bridge.errors import BridgeError
from airlie.serve.bridge.job_bus import HubJob, JobBusClient
from airlie.serve.bridge.kinds import handler_for
from airlie.serve.bridge.session import credential_env, serve_api_key, worker_id, worker_secret

log = logging.getLogger(__name__)


@dataclass
class BridgeRunner:
    cfg: AirlieConfig
    hub: JobBusClient
    serve: ServeClient
    worker: str
    clock: Clock
    bridge: BridgeSpec
    stop: asyncio.Event
    jobs_processed: int = 0
    last_job_finished: float = 0.0
    busy: Callable[[], Awaitable[str | None]] | None = None
    _last_busy_logged: str | None = None

    async def process_job(self, job: HubJob) -> None:
        log.info("claimed hub job %s kind=%s tenant=%s", job.id, job.kind, job.tenant_id)
        try:
            handler = handler_for(job.kind, self.bridge, self.serve, clock=self.clock)
        except BridgeError as e:
            await self.hub.post_result(job.id, self.worker, status="failed", error_message=str(e))
            return
        outcome = await handler.run(job.payload)
        await self.hub.post_result(
            job.id,
            self.worker,
            status=outcome.status,
            result=outcome.result,
            error_message=outcome.error_message,
        )
        self.jobs_processed += 1
        self.last_job_finished = self.clock.wall()

    async def tick(self) -> bool:
        """One claim attempt; True when a job was processed."""
        if self.busy is not None:
            reason = await self.busy()
            if reason is not None:
                if reason != self._last_busy_logged:
                    log.info("not claiming: %s", reason)
                self._last_busy_logged = reason
                return False
            self._last_busy_logged = None
        job = await self.hub.claim(self.worker)
        if job is None:
            return False
        await self.process_job(job)
        return True

    async def run_once(self) -> bool:
        return await self.tick()

    async def _wait_claim_poll(self) -> None:
        """Sleep until the next claim attempt, or until `stop` is set."""
        poll = self.bridge.claim_poll_s
        if poll <= 0 or self.stop.is_set():
            return
        sleep_task = asyncio.create_task(self.clock.sleep(poll))
        stop_task = asyncio.create_task(self.stop.wait())
        _, pending = await asyncio.wait({sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def run_until_stopped(self, *, max_cycles: int | None = None) -> None:
        cycles = 0
        while not self.stop.is_set():
            if max_cycles is not None and cycles >= max_cycles:
                break
            worked = await self.tick()
            cycles += 1
            if not worked:
                await self._wait_claim_poll()


async def run_bridge(
    cfg: AirlieConfig,
    *,
    env: dict[str, str] | None = None,
    once: bool = False,
    hub_transport: Any = None,
    serve_transport: Any = None,
    clock: Clock | None = None,
    stop: asyncio.Event | None = None,
    max_cycles: int | None = None,
) -> int:
    """Start the bridge loop (or one claim cycle when `once`). Returns jobs processed."""
    bridge = cfg.bridge
    if not bridge.job_bus_url:
        raise BridgeError("bridge.job_bus_url is not set")
    creds = credential_env(cfg, env)
    secret = worker_secret(bridge, creds)
    wid = worker_id(bridge, creds)
    hub = JobBusClient(bridge.job_bus_url, secret, transport=hub_transport)
    serve = ServeClient(bridge.serve_url, api_key=serve_api_key(bridge, creds), transport=serve_transport)
    runner = BridgeRunner(
        cfg=cfg,
        hub=hub,
        serve=serve,
        worker=wid,
        clock=clock or RealClock(),
        bridge=bridge,
        stop=stop or asyncio.Event(),
    )
    probe_http = httpx.AsyncClient()

    async def busy() -> str | None:
        return await busy_reason(
            cfg, http=probe_http, now=runner.clock.wall(), own_last_used=runner.last_job_finished
        )

    runner.busy = busy
    try:
        if once:
            await runner.run_once()
        else:
            await runner.run_until_stopped(max_cycles=max_cycles)
    finally:
        await hub.aclose()
        await serve.aclose()
        await probe_http.aclose()
    return runner.jobs_processed
