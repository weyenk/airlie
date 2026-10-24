"""The live feed: `Feed` fans changes out to every open `GET /v1/events` stream (hints only: the page
refetches through the existing GET routes), from any thread."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from airlie.serve.app import create_app
from airlie.serve.feed import Change, Feed, event_stream
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream, cfg


async def test_a_subscriber_gets_what_is_published_in_order() -> None:
    feed = Feed()
    async with feed.subscribe() as sub:
        feed.publish("board", "AIR-17")
        feed.publish("runs")
        it = aiter(sub)
        assert (await anext(it)).key == "AIR-17"
        second = await anext(it)
        assert (second.topic, second.key) == ("runs", None) and second.id > 1


async def test_the_stream_opens_with_connected_then_sends_changes_and_pings() -> None:
    feed = Feed()
    stream = event_stream(feed, heartbeat_s=0.05)
    assert await anext(stream) == b": connected\n\n"
    feed.publish("board", "AIR-1")
    assert await anext(stream) == b'id: 1\nevent: change\ndata: {"topic": "board", "key": "AIR-1"}\n\n'
    assert await anext(stream) == b": ping\n\n"
    await stream.aclose()
    assert feed.subscribers == 0


def test_events_needs_sign_in_and_streams_with_proxy_safe_headers(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    remote = TestClient(app, client=("10.0.0.5", 1))
    assert remote.get("/v1/events").status_code == 401
    app.state.feed.close()  # the stream ends after ": connected"
    r = TestClient(app).get("/v1/events", headers={"Authorization": "Bearer k"})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert r.text.startswith(": connected")


async def test_every_subscriber_gets_every_change_and_none_is_replayed() -> None:
    feed = Feed()
    feed.publish("board", "AIR-0")  # nobody is listening: the feed keeps no history
    async with feed.subscribe() as a, feed.subscribe() as b:
        assert feed.subscribers == 2
        feed.publish("jobs", "j1")
        assert await anext(aiter(a)) == Change(2, "jobs", "j1")
        assert await anext(aiter(b)) == Change(2, "jobs", "j1")
    assert feed.subscribers == 0


async def test_a_subscriber_is_counted_only_while_it_is_inside_its_block() -> None:
    feed = Feed()
    assert feed.subscribers == 0
    async with feed.subscribe():
        assert feed.subscribers == 1
    assert feed.subscribers == 0


async def test_publish_is_safe_from_any_thread_and_keeps_one_order() -> None:
    """The board routes run in FastAPI's thread pool: eight threads publish, and the one subscriber sees
    every change once, in id order."""
    feed = Feed()
    per_thread, threads = 50, 8
    async with feed.subscribe() as sub:

        def burst(n: int) -> None:
            for i in range(per_thread):
                feed.publish("board", f"{n}-{i}")

        await asyncio.gather(*(asyncio.to_thread(burst, n) for n in range(threads)))
        it = aiter(sub)
        seen = [await asyncio.wait_for(anext(it), 5) for _ in range(per_thread * threads)]
    assert [c.id for c in seen] == list(range(1, per_thread * threads + 1))
    assert len({c.key for c in seen}) == per_thread * threads


async def test_a_thread_that_publishes_wakes_a_waiting_subscriber() -> None:
    feed = Feed()
    async with feed.subscribe() as sub:
        waiting = asyncio.ensure_future(anext(aiter(sub)))
        await asyncio.sleep(0)  # the subscriber is parked on its queue
        await asyncio.to_thread(feed.publish, "runs", None)
        assert (await asyncio.wait_for(waiting, 5)).topic == "runs"


async def test_close_ends_every_subscription_after_what_was_already_published() -> None:
    feed = Feed()
    async with feed.subscribe() as sub:
        feed.publish("board", "AIR-3")
        feed.close()
        it = aiter(sub)
        assert (await anext(it)).key == "AIR-3"
        for _ in range(2):  # it stays ended
            try:
                await anext(it)
            except StopAsyncIteration:
                continue
            raise AssertionError("a closed feed's subscription must end")


async def test_close_from_another_thread_ends_a_waiting_subscription() -> None:
    feed = Feed()
    async with feed.subscribe() as sub:
        waiting = asyncio.ensure_future(anext(aiter(sub), None))
        await asyncio.sleep(0)
        await asyncio.to_thread(feed.close)
        assert await asyncio.wait_for(waiting, 5) is None


async def test_a_closed_feed_yields_connected_and_ends_and_leaves_no_subscriber() -> None:
    feed = Feed()
    feed.close()
    feed.publish("board", "AIR-4")  # nothing to deliver to, and no error
    chunks = [chunk async for chunk in event_stream(feed)]
    assert chunks == [b": connected\n\n"]
    assert feed.subscribers == 0


async def test_a_stream_that_is_cancelled_leaves_no_subscriber() -> None:
    feed = Feed()
    stream = event_stream(feed)
    assert await anext(stream) == b": connected\n\n"
    assert feed.subscribers == 1
    parked = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    parked.cancel()  # what Starlette does to the response when the client goes away
    await asyncio.gather(parked, return_exceptions=True)
    assert feed.subscribers == 0


async def test_a_change_that_lands_beside_a_ping_is_not_lost() -> None:
    feed = Feed()
    stream = event_stream(feed, heartbeat_s=0.01)
    assert await anext(stream) == b": connected\n\n"
    assert await anext(stream) == b": ping\n\n"
    feed.publish("board", "AIR-5")
    frame = await anext(stream)
    while frame == b": ping\n\n":  # a slow machine may add a ping first
        frame = await anext(stream)
    assert frame == b'id: 1\nevent: change\ndata: {"topic": "board", "key": "AIR-5"}\n\n'
    await stream.aclose()


async def test_a_key_is_sent_as_json_so_odd_text_cannot_break_the_frame() -> None:
    feed = Feed()
    stream = event_stream(feed)
    assert await anext(stream) == b": connected\n\n"
    feed.publish("board", 'a"b\nc')
    frame = await anext(stream)
    assert frame.count(b"\n") == 4  # id, event, data and the blank line: the newline in the key is escaped
    assert frame.endswith(b'"key": "a\\"b\\nc"}\n\n')
    await stream.aclose()


def test_publishing_after_a_subscriber_s_loop_is_gone_neither_raises_nor_counts_it() -> None:
    """A stream whose event loop was torn down (a test client, a stopped server) must not break the
    publisher, which is a board route in a worker thread."""
    feed = Feed()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(feed.subscribe().__aenter__())
    loop.close()
    assert feed.subscribers == 1
    feed.publish("board", "AIR-6")
    assert feed.subscribers == 0


async def test_the_events_route_stops_counting_a_client_that_disconnects(tmp_path: Path) -> None:
    """Through the app, middleware and all: the first chunk is `: connected`, a disconnect ends the
    response, and the feed is back to no subscribers (the watcher stops polling then)."""
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    feed: Feed = app.state.feed
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/v1/events",
        "raw_path": b"/v1/events",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"testserver"), (b"authorization", b"Bearer k")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    call = asyncio.ensure_future(app(scope, inbox.get, sent.put))
    start = await asyncio.wait_for(sent.get(), 5)
    assert start["status"] == 200
    assert (await asyncio.wait_for(sent.get(), 5))["body"] == b": connected\n\n"
    assert feed.subscribers == 1
    feed.publish("board", "AIR-7")
    assert b"AIR-7" in (await asyncio.wait_for(sent.get(), 5))["body"]
    inbox.put_nowait({"type": "http.disconnect"})
    await asyncio.wait_for(call, 5)
    assert feed.subscribers == 0


async def test_on_subscribed_runs_after_the_stream_has_subscribed_and_before_it_says_connected() -> None:
    """What lets the watcher take its baseline after the page is counted and before the page is told it is
    connected: a write after `: connected` is never folded into that baseline."""
    feed = Feed()
    seen: list[int] = []
    stream = event_stream(feed, on_subscribed=lambda: seen.append(feed.subscribers))
    assert seen == []  # nothing runs until the stream is read
    assert await anext(stream) == b": connected\n\n"
    assert seen == [1]  # subscribed by then
    await stream.aclose()
    assert feed.subscribers == 0
