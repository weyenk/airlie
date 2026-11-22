"""Fetch model ids from a remote OpenAI-style engine."""

from __future__ import annotations

import httpx


class LiveModelsError(RuntimeError):
    """The upstream engine did not return a model list."""


async def fetch_live_model_ids(
    base_url: str,
    api_key: str | None,
    *,
    http: httpx.AsyncClient,
) -> list[str]:
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = await http.get(url, headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise LiveModelsError(f"GET {url} failed with {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise LiveModelsError(f"GET {url} failed: {e}") from e
    body = response.json()
    return [str(item["id"]) for item in body.get("data") or [] if "id" in item]
