from __future__ import annotations

import httpx
import pytest

from airlie.models.live import LiveModelsError, fetch_live_model_ids


async def test_fetch_live_model_ids_reads_openai_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as http:
        ids = await fetch_live_model_ids("http://engine/v1", "key", http=http)
    assert ids == ["a", "b"]


async def test_fetch_live_model_ids_raises_on_http_error() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(LiveModelsError, match="503"):
            await fetch_live_model_ids("http://engine/v1", None, http=http)


async def test_fetch_live_model_ids_raises_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(LiveModelsError, match="refused"):
            await fetch_live_model_ids("http://engine/v1", None, http=http)
