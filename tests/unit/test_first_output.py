"""The engine's own time to first output, sent on the stream's usage chunk (patches/first_output.py)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from vllm_mlx.metrics import InferenceTracker

from airlie.engine.patches import first_output
from airlie.engine.patches.first_output import FIELD, stamp, wrap


def chunk(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def test_stamp_adds_the_time_to_a_chunk_with_usage_and_leaves_the_rest() -> None:
    usage = chunk({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}})
    assert json.loads(stamp(usage, 1234.56)[6:])[FIELD] == 1234.6
    assert stamp(usage, 1234.56).endswith("\n\n")
    content = chunk({"choices": [{"delta": {"content": "a"}}]})
    for same in (content, chunk({"usage": None}), "data: [DONE]\n\n"):
        assert stamp(same, 5.0) == same
    assert stamp(usage, None) == usage  # no output yet: nothing to say


async def test_a_tool_call_reply_carries_the_time_its_first_output_left_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vllm-mlx's tool parser holds a call back until it is whole, so the client sees its first delta only at
    the end; the engine's first output is the end of the prompt's processing."""
    first_output.apply()
    clock = iter([11.5, 99.0])
    monkeypatch.setattr(first_output.time, "perf_counter", lambda: next(clock))
    tracker = InferenceTracker(None, "chat_completions", True, start_time=10.0)  # metrics off: still timed

    async def original(*a: Any, metrics_tracker: Any = None, **kw: Any) -> AsyncIterator[str]:
        yield chunk({"choices": [{"delta": {"role": "assistant"}}]})
        metrics_tracker.observe_ttft()  # the first output: 11.5
        metrics_tracker.observe_ttft()  # later outputs move nothing
        yield chunk({"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]})
        yield chunk({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}})
        yield "data: [DONE]\n\n"

    out = [c async for c in wrap(original)(None, [], None, metrics_tracker=tracker)]
    assert json.loads(out[2][6:])[FIELD] == 1500.0
    assert FIELD not in out[0] and FIELD not in out[1] and out[3] == "data: [DONE]\n\n"


async def test_a_stream_without_a_tracker_passes_through_untouched() -> None:
    async def original(*a: Any, **kw: Any) -> AsyncIterator[str]:
        yield chunk({"choices": [], "usage": {"prompt_tokens": 1}})

    out = [c async for c in wrap(original)(None, [], None)]
    assert out == [chunk({"choices": [], "usage": {"prompt_tokens": 1}})]


def test_apply_patches_the_server_once() -> None:
    import vllm_mlx.server as server

    assert first_output.apply() is True
    patched = server.stream_chat_completion
    assert first_output.apply() is True and server.stream_chat_completion is patched
