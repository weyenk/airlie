"""SSE frame parsing for OpenAI-compatible streams: stop at [DONE], surface error frames, never hang on truncation."""

from collections.abc import AsyncIterator

import pytest

from airlie.engine.sse import iter_sse
from airlie.engine.types import EngineError


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


async def collect(it: AsyncIterator[dict]) -> list[dict]:
    return [x async for x in it]


async def test_parses_data_frames_and_stops_at_done() -> None:
    body = b'data: {"a": 1}\n\ndata: {"a": 2}\n\ndata: [DONE]\n\ndata: {"a": 3}\n\n'
    assert await collect(iter_sse(chunks(body))) == [{"a": 1}, {"a": 2}]


async def test_frames_split_across_chunks_are_reassembled() -> None:
    assert await collect(iter_sse(chunks(b'data: {"x": "he', b'llo"}\n\ndata: [DO', b"NE]\n\n"))) == [
        {"x": "hello"}
    ]


async def test_ignores_comments_and_blank_lines() -> None:
    body = b': keep-alive\n\n\n\ndata: {"ok": true}\n\ndata: [DONE]\n\n'
    assert await collect(iter_sse(chunks(body))) == [{"ok": True}]


async def test_error_frame_raises_engine_error_with_message() -> None:
    body = b'data: {"error": {"message": "model busy", "type": "server_error"}}\n\n'
    with pytest.raises(EngineError, match="model busy"):
        await collect(iter_sse(chunks(body)))


async def test_truncated_stream_without_done_raises() -> None:
    body = b'data: {"a": 1}\n\ndata: {"a": 2}\n\n'
    with pytest.raises(EngineError, match=r"stream ended without \[DONE\]"):
        await collect(iter_sse(chunks(body)))


async def test_truncated_stream_can_be_tolerated_when_asked() -> None:
    body = b'data: {"a": 1}\n\n'
    assert await collect(iter_sse(chunks(body), require_done=False)) == [{"a": 1}]


async def test_invalid_json_frame_raises() -> None:
    with pytest.raises(EngineError, match="not JSON"):
        await collect(iter_sse(chunks(b"data: {nope\n\n")))
