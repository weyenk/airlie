"""HTTPS client for the Job Bus worker routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from airlie.serve.bridge.errors import BridgeError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HubJob:
    id: str
    kind: str
    tenant_id: str
    payload: dict[str, Any]
    worker_id: str | None


class JobBusClient:
    """Short-poll claim and terminal result posting."""

    def __init__(
        self,
        base_url: str,
        worker_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {worker_secret}"}
        self._http = httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def claim(self, worker_id: str) -> HubJob | None:
        res = await self._http.post("/jobs/claim", json={"worker_id": worker_id})
        if res.status_code == 204:
            return None
        if res.status_code == 401:
            raise BridgeError("Job Bus rejected the worker secret (401)")
        if res.status_code != 200:
            raise BridgeError(f"Job Bus claim failed: {res.status_code} {res.text[:300]}")
        job = res.json().get("job")
        if not isinstance(job, dict):
            raise BridgeError("Job Bus claim returned no job object")
        return HubJob(
            id=str(job["id"]),
            kind=str(job["kind"]),
            tenant_id=str(job.get("tenant_id", "")),
            payload=dict(job.get("payload") or {}),
            worker_id=str(job["worker_id"]) if job.get("worker_id") else None,
        )

    async def post_result(
        self,
        job_id: str,
        worker_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"worker_id": worker_id, "status": status}
        if result is not None:
            body["result"] = result
        if error_message is not None:
            body["error_message"] = error_message
        path = f"/jobs/{job_id}/result"
        res = await self._http.post(path, json=body)
        if res.status_code not in (200, 201):
            raise BridgeError(f"Job Bus result failed: {res.status_code} {res.text[:300]}")
        log.info("hub job %s → %s", job_id, status)
