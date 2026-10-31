"""Loopback client for `task serve` jobs API."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from airlie.serve.bridge.errors import BridgeError

log = logging.getLogger(__name__)


def job_path(job_id: str) -> str:
    return f"/v1/airlie/jobs/{quote(job_id, safe='')}"


class ServeClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers: dict[str, str] = {}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def start_run(self, body: dict[str, Any]) -> str:
        res = await self._http.post("/v1/airlie/runs", json=body)
        if res.status_code == 202:
            data = res.json()
            return str(data["job_id"])
        if res.status_code == 401:
            detail = (res.text or "").strip().replace("\n", " ")[:160]
            raise BridgeError(
                "serve rejected the request (401)"
                + (f": {detail}" if detail else " — check AIRLIE_SERVE_API_KEY matches task serve")
            )
        raise BridgeError(f"serve POST /v1/airlie/runs failed: {res.status_code} {res.text[:400]}")

    async def poll_job(self, job_id: str, *, wait: float = 0.0) -> dict[str, Any]:
        res = await self._http.get(job_path(job_id), params={"wait": wait})
        if res.status_code == 404:
            raise ServeJobNotFound(job_id)
        if not res.is_success:
            raise BridgeError(f"serve GET job failed: {res.status_code} {res.text[:400]}")
        data = res.json()
        if not isinstance(data, dict):
            raise BridgeError("serve job response is not an object")
        return data


class ServeJobNotFound(BridgeError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"serve job {job_id!r} not found (404)")
        self.job_id = job_id
