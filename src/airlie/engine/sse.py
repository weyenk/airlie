"""Minimal Server-Sent-Events reader for OpenAI-compatible `stream: true` responses."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from airlie.engine.types import EngineError

DONE = "[DONE]"


async def iter_sse(
    byte_chunks: AsyncIterator[bytes], *, require_done: bool = True
) -> AsyncIterator[dict[str, Any]]:
    """Yield the JSON object of every `data:` frame until `[DONE]`.

    Raises EngineError on an `{"error": …}` frame, on non-JSON data, and (by default) when the stream ends
    without `[DONE]`, so a dropped connection never looks like a clean finish."""
    buf = b""
    done = False
    async for chunk in byte_chunks:
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            data = _data_of(frame)
            if data is None:
                continue
            if data == DONE:
                done = True
                return
            obj = _parse(data)
            if isinstance(obj.get("error"), dict):
                raise EngineError(f"engine error: {obj['error'].get('message', obj['error'])}")
            yield obj
    if buf.strip():
        data = _data_of(buf)
        if data == DONE:
            return
        if data is not None:
            obj = _parse(data)
            if isinstance(obj.get("error"), dict):
                raise EngineError(f"engine error: {obj['error'].get('message', obj['error'])}")
            yield obj
    if require_done and not done:
        raise EngineError("stream ended without [DONE]")


def _data_of(frame: bytes) -> str | None:
    lines = [ln for ln in frame.decode("utf-8", "replace").split("\n") if ln and not ln.startswith(":")]
    data = [ln[5:].lstrip() for ln in lines if ln.startswith("data:")]
    return "\n".join(data) if data else None


def _parse(data: str) -> dict[str, Any]:
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as e:
        raise EngineError(f"SSE frame is not JSON: {data[:80]!r}") from e
    if not isinstance(obj, dict):
        raise EngineError(f"SSE frame is not an object: {data[:80]!r}")
    return obj
