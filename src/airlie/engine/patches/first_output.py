"""vllm-mlx 0.5.0 knows when a request's first output left the engine, and tells only Prometheus.

The client times the prompt's processing as the wait for its first delta. A tool call is no such delta: the
streaming tool parser holds a call back until it is whole and sends it at the end, so for a reply that is only
a call the wait spans the whole decode. AIR-46's refine (2026-10-31, eight read_file steps) read 2.0 s of
"prefill" and 0.1 s of generation for 51 tokens, 1,015 tok/s, and the performance review of 2026-10-23 found
the same split in the coder's numbers.

`stream_chat_completion` calls its request's `InferenceTracker.observe_ttft()` on the engine's first output,
before any parser, and the tracker's `start_time` is the request's arrival. This keeps that moment on the
tracker (with metrics off too, when the tracker has no collector) and adds it to the stream's usage chunk as
`airlie_ttft_ms`, milliseconds from arrival to first output, which `HttpEngine` reads in place of its own."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

FIELD = "airlie_ttft_ms"
_FIRST = "_airlie_first_output"
_PREFIX = "data: "
_applied = False


def stamp(chunk: str, ttft_ms: float | None) -> str:
    """`chunk` with `ttft_ms` added when it is a data chunk that carries usage; any other chunk as it is."""
    if ttft_ms is None or not chunk.startswith(_PREFIX) or '"usage"' not in chunk:
        return chunk
    try:
        obj = json.loads(chunk[len(_PREFIX) :])
    except ValueError:
        return chunk
    if not isinstance(obj, dict) or not obj.get("usage"):
        return chunk
    obj[FIELD] = round(ttft_ms, 1)
    return f"{_PREFIX}{json.dumps(obj)}\n\n"


def wrap(original: Callable[..., AsyncIterator[str]]) -> Callable[..., AsyncIterator[str]]:
    """`stream_chat_completion` that stamps its usage chunks with the request's time to first output."""

    async def stream_chat_completion(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        tracker: Any = kwargs.get("metrics_tracker")
        async for chunk in original(*args, **kwargs):
            first = getattr(tracker, _FIRST, None)
            yield stamp(chunk, None if first is None else (first - tracker.start_time) * 1000.0)

    return stream_chat_completion


def apply() -> bool:
    """Patch vllm-mlx once: the tracker keeps its first output, the chat stream sends it. True when done."""
    global _applied
    if _applied:
        return True
    import vllm_mlx.metrics as metrics
    import vllm_mlx.server as server

    observe = metrics.InferenceTracker.observe_ttft

    def observe_ttft(self: Any) -> None:
        if getattr(self, _FIRST, None) is None:
            setattr(self, _FIRST, time.perf_counter())
        observe(self)

    metrics.InferenceTracker.observe_ttft = observe_ttft
    server.stream_chat_completion = wrap(server.stream_chat_completion)
    _applied = True
    return True
