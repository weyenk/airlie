"""SSE parsing for OpenAI-compatible streams: stop at [DONE], surface errors, never hang on truncation."""

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


async def test_unterminated_final_frame_is_still_yielded() -> None:
    body = b'data: {"a": 1}\n\ndata: {"a": 2}'
    assert await collect(iter_sse(chunks(body), require_done=False)) == [{"a": 1}, {"a": 2}]


async def test_unterminated_final_done_counts_as_done() -> None:
    assert await collect(iter_sse(chunks(b'data: {"a": 1}\n\ndata: [DONE]'))) == [{"a": 1}]


async def test_unterminated_final_frame_without_done_still_raises_by_default() -> None:
    with pytest.raises(EngineError, match=r"stream ended without \[DONE\]"):
        await collect(iter_sse(chunks(b'data: {"a": 1}')))


async def test_whitespace_only_tail_is_ignored() -> None:
    assert await collect(iter_sse(chunks(b'data: {"a": 1}\n\ndata: [DONE]\n\n   \n'))) == [{"a": 1}]
    assert await collect(iter_sse(chunks(b'data: {"a": 1}\n\n  \n'), require_done=False)) == [{"a": 1}]


async def test_unterminated_final_comment_only_tail_is_ignored() -> None:
    assert await collect(iter_sse(chunks(b'data: {"a": 1}\n\n: ping'), require_done=False)) == [{"a": 1}]


async def test_unterminated_final_error_frame_raises() -> None:
    with pytest.raises(EngineError, match="engine error: boom"):
        await collect(iter_sse(chunks(b'data: {"error": {"message": "boom"}}'), require_done=False))
    with pytest.raises(EngineError) as ei:
        await collect(iter_sse(chunks(b'data: {"error": {"code": 7}}'), require_done=False))
    assert str(ei.value) == "engine error: {'code': 7}"


async def test_unterminated_final_invalid_json_raises() -> None:
    with pytest.raises(EngineError, match="not JSON"):
        await collect(iter_sse(chunks(b"data: {nope"), require_done=False))


async def test_error_without_message_reports_the_error_dict() -> None:
    with pytest.raises(EngineError, match=r"engine error: \{'code': 7\}"):
        await collect(iter_sse(chunks(b'data: {"error": {"code": 7}}\n\n')))


async def test_error_message_is_exact() -> None:
    with pytest.raises(EngineError) as ei:
        await collect(iter_sse(chunks(b'data: {"error": {"message": "model busy"}}\n\n')))
    assert str(ei.value) == "engine error: model busy"


async def test_non_dict_error_field_is_ordinary_data() -> None:
    body = b'data: {"error": "just a string", "n": 1}\n\ndata: [DONE]\n\n'
    assert await collect(iter_sse(chunks(body))) == [{"error": "just a string", "n": 1}]
    tail = b'data: {"error": null}'
    assert await collect(iter_sse(chunks(tail), require_done=False)) == [{"error": None}]


async def test_many_frames_in_one_chunk_and_chunks_after_done_are_never_read() -> None:
    consumed: list[int] = []

    async def source() -> AsyncIterator[bytes]:
        consumed.append(1)
        yield b'data: {"a": 1}\n\ndata: {"a": 2}\n\ndata: [DONE]\n\n'
        consumed.append(2)
        yield b'data: {"a": 3}\n\n'

    assert await collect(iter_sse(source())) == [{"a": 1}, {"a": 2}]
    assert consumed == [1]


async def test_done_without_space_and_data_without_space() -> None:
    assert await collect(iter_sse(chunks(b'data:{"a":1}\n\ndata:[DONE]\n\n'))) == [{"a": 1}]


async def test_multiline_data_is_joined_with_newline() -> None:
    assert await collect(iter_sse(chunks(b'data: {"a":\ndata: 1}\n\ndata: [DONE]\n\n'))) == [{"a": 1}]


async def test_non_data_fields_are_ignored() -> None:
    body = b'event: message\nid: 7\ndata: {"a": 1}\nretry: 5\n\nevent: ping\n\ndata: [DONE]\n\n'
    assert await collect(iter_sse(chunks(body))) == [{"a": 1}]


async def test_comment_lines_inside_a_frame_are_dropped() -> None:
    assert await collect(iter_sse(chunks(b': c\ndata: {"a": 1}\n\ndata: [DONE]\n\n'))) == [{"a": 1}]


async def test_invalid_utf8_is_replaced_not_fatal() -> None:
    body = b'data: {"s": "a\xffb"}\n\ndata: [DONE]\n\n'
    assert await collect(iter_sse(chunks(body))) == [{"s": "a�b"}]


async def test_utf8_split_across_chunks() -> None:
    word = "héllo".encode()
    parts = (b'data: {"s": "' + word[:2], word[2:] + b'"}\n\ndata: [DONE]\n\n')
    assert await collect(iter_sse(chunks(*parts))) == [{"s": "héllo"}]


async def test_non_object_json_frame_raises() -> None:
    with pytest.raises(EngineError, match="not an object"):
        await collect(iter_sse(chunks(b"data: [1, 2]\n\n")))
    with pytest.raises(EngineError, match="not an object"):
        await collect(iter_sse(chunks(b"data: 7\n\n")))


async def test_bad_frame_error_quotes_the_data_truncated_to_80_chars() -> None:
    long = "x" * 200
    with pytest.raises(EngineError) as ei:
        await collect(iter_sse(chunks(f"data: {long}\n\n".encode())))
    assert str(ei.value) == f"SSE frame is not JSON: {('x' * 80)!r}"
    with pytest.raises(EngineError) as ei:
        await collect(iter_sse(chunks(b"data: [" + b"1," * 60 + b"1]\n\n")))
    assert str(ei.value) == f"SSE frame is not an object: {('[' + '1,' * 60 + '1]')[:80]!r}"


async def test_empty_stream_with_require_done_raises_and_without_is_empty() -> None:
    with pytest.raises(EngineError) as ei:
        await collect(iter_sse(chunks()))
    assert str(ei.value) == "stream ended without [DONE]"
    assert await collect(iter_sse(chunks(), require_done=False)) == []


async def test_stream_that_finished_with_done_does_not_raise_even_if_require_done() -> None:
    assert await collect(iter_sse(chunks(b"data: [DONE]\n\n"), require_done=True)) == []
