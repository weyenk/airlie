"""The live feed: `Feed` fans changes out to every open `GET /v1/events` stream (hints only: the page
refetches through the existing GET routes), from any thread."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from airlie.auth.principal import Principal
from airlie.board import BoardStore
from airlie.board.api import board_router
from airlie.serve.app import create_app
from airlie.serve.feed import Change, Feed, event_stream
from airlie.serve.jobs import Jobs
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


# ---- in-process changes publish at once: the board routes and the job manager ----

SIGNED_IN = Principal("api-key", "api-key", "admin", "api-key")  # as the app's key middleware sets it


@pytest.fixture
def board_store(tmp_path: Path) -> Iterator[BoardStore]:
    store = BoardStore(tmp_path / "b.sqlite")
    yield store
    store.close()


def board_client(
    store: BoardStore, seen: list[tuple[str, str | None]], principal: Principal | None = None
) -> TestClient:
    """The board router on its own, publishing into `seen`; `principal` stands in for the key middleware."""
    app = FastAPI()
    app.include_router(board_router(store, lambda topic, key: seen.append((topic, key))))
    if principal is not None:

        @app.middleware("http")
        async def _sign_in(request: Request, call_next: Callable[..., Any]) -> Any:
            request.state.principal = principal
            return await call_next(request)

    return TestClient(app)


def test_each_board_write_publishes_its_key(board_store: BoardStore) -> None:
    seen: list[tuple[str, str | None]] = []
    c = board_client(board_store, seen)
    key = c.post("/v1/board/items", json={"level": "initiative", "title": "x"}).json()["key"]
    assert seen == [("board", key)]  # the create, with the key the store gave it
    assert c.patch(f"/v1/board/items/{key}", json={"title": "y"}).status_code == 200
    assert c.post(f"/v1/board/items/{key}/move", json={"status": "doing"}).status_code == 200
    assert c.post(f"/v1/board/items/{key}/comments", json={"text": "hi"}).status_code == 200
    assert seen == [("board", key)] * 4


def test_an_edited_comment_publishes_its_item_s_key(board_store: BoardStore) -> None:
    seen: list[tuple[str, str | None]] = []
    c = board_client(board_store, seen, SIGNED_IN)
    key = c.post("/v1/board/items", json={"level": "initiative", "title": "x"}).json()["key"]
    board_store.comment(key, "api-key", "first draft")  # the signed-in person's own comment
    seen.clear()
    r = c.patch(f"/v1/board/items/{key}/comments/1", json={"text": "revised"})
    assert r.status_code == 200 and r.json()["comments"][0]["text"] == "revised"
    assert seen == [("board", key)]


def test_a_rejected_board_write_publishes_nothing(board_store: BoardStore) -> None:
    """The hint follows a write that happened: a refused request (404, 403, 401, 422) changes nothing, so a
    page has nothing to refetch."""
    seen: list[tuple[str, str | None]] = []
    c = board_client(board_store, seen, SIGNED_IN)
    key = c.post("/v1/board/items", json={"level": "initiative", "title": "x"}).json()["key"]
    board_store.comment(key, "someone-else", "theirs")
    seen.clear()
    refused = {
        "create under a parent that is not there": c.post(
            "/v1/board/items", json={"level": "epic", "title": "e", "parent": "AIR-99"}
        ),
        "create with a level that is no level": c.post(
            "/v1/board/items", json={"level": "saga", "title": "e"}
        ),
        "update of an unknown item": c.patch("/v1/board/items/AIR-99", json={"title": "y"}),
        "update to a parent that is not there": c.patch(f"/v1/board/items/{key}", json={"parent": "AIR-99"}),
        "move of an unknown item": c.post("/v1/board/items/AIR-99/move", json={"status": "doing"}),
        "move above a card that is not in the column": c.post(
            f"/v1/board/items/{key}/move", json={"status": "doing", "before": "AIR-99"}
        ),
        "comment on an unknown item": c.post("/v1/board/items/AIR-99/comments", json={"text": "hi"}),
        "edit of an unknown item's comment": c.patch("/v1/board/items/AIR-99/comments/1", json={"text": "z"}),
        "edit of an unknown comment": c.patch(f"/v1/board/items/{key}/comments/99", json={"text": "z"}),
        "edit of someone else's comment": c.patch(f"/v1/board/items/{key}/comments/1", json={"text": "z"}),
    }
    assert {name: r.status_code for name, r in refused.items()} == {
        "create under a parent that is not there": 422,
        "create with a level that is no level": 422,
        "update of an unknown item": 404,
        "update to a parent that is not there": 422,
        "move of an unknown item": 404,
        "move above a card that is not in the column": 422,
        "comment on an unknown item": 404,
        "edit of an unknown item's comment": 404,
        "edit of an unknown comment": 404,
        "edit of someone else's comment": 403,
    }
    assert seen == []
    # and a request that is not signed in at all
    assert (
        board_client(board_store, seen)
        .patch(f"/v1/board/items/{key}/comments/1", json={"text": "z"})
        .status_code
        == 401
    )
    assert seen == []


async def test_a_job_publishes_when_it_starts_and_when_it_ends(tmp_path: Path) -> None:
    seen: list[tuple[str, str | None]] = []
    jobs = Jobs(publish=lambda topic, key: seen.append((topic, key)))

    async def work() -> dict[str, Any]:
        return {}

    job = jobs.start("run", "r1", tmp_path / "e.jsonl", work())
    assert seen == [("jobs", "r1")]  # at once, with the job already listed as running
    await job.task
    await asyncio.sleep(0)  # the done callback runs on the loop's next turn
    assert seen == [("jobs", "r1"), ("jobs", "r1")]


async def test_a_failed_or_cancelled_job_publishes_when_it_ends_too(tmp_path: Path) -> None:
    seen: list[tuple[str, str | None]] = []
    jobs = Jobs(publish=lambda topic, key: seen.append((topic, key)))

    async def boom() -> dict[str, Any]:
        raise RuntimeError("engine fell over")

    async def hang() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    failed = jobs.start("run", "r1", tmp_path / "e.jsonl", boom())
    with pytest.raises(RuntimeError):
        await failed.task
    await asyncio.sleep(0)
    assert seen == [("jobs", "r1")] * 2 and failed.state == "failed"
    hung = jobs.start("run", "r2", tmp_path / "e.jsonl", hang())
    assert jobs.cancel("r2") is True
    await asyncio.wait([hung.task])
    await asyncio.sleep(0)
    assert seen[2:] == [("jobs", "r2")] * 2 and hung.state == "cancelled"


async def test_a_job_that_is_refused_publishes_nothing(tmp_path: Path) -> None:
    seen: list[tuple[str, str | None]] = []
    jobs = Jobs(publish=lambda topic, key: seen.append((topic, key)))

    async def hang() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    running = jobs.start("run", "r1", tmp_path / "e.jsonl", hang())
    seen.clear()
    with pytest.raises(ValueError, match="already running"):
        jobs.start("run", "r1", tmp_path / "e.jsonl", hang())
    await asyncio.sleep(0)
    assert seen == []
    running.task.cancel()


async def test_jobs_without_a_feed_start_and_end_quietly(tmp_path: Path) -> None:
    async def work() -> dict[str, Any]:
        return {"ok": True}

    job = Jobs().start("run", "r1", tmp_path / "e.jsonl", work())
    assert await job.task == {"ok": True}


async def test_the_app_wires_the_board_and_the_jobs_to_its_feed(tmp_path: Path) -> None:
    """Through create_app: a board write and a story job reach a subscriber of app.state.feed at once,
    with no watcher running (nothing opened /v1/events)."""
    events = tmp_path / "story-events.jsonl"

    async def fake_story_job(cfg: Any, **kw: Any) -> tuple[str, Path, Any]:
        async def work() -> dict[str, Any]:
            return {"stage": "review"}

        return "story-1", events, work()

    app = create_app(
        cfg(tmp_path),
        transport=httpx.MockTransport(FakeUpstream()),
        api_key="k",
        story_job=fake_story_job,
    )
    feed: Feed = app.state.feed
    auth = {"Authorization": "Bearer k"}
    async with (
        feed.subscribe() as sub,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=auth) as c,
    ):
        changes = aiter(sub)
        r = await c.post("/v1/board/items", json={"level": "initiative", "title": "x"})
        assert r.status_code == 201
        change = await asyncio.wait_for(anext(changes), 5)
        assert (change.topic, change.key) == ("board", r.json()["key"])
        r = await c.post("/v1/airlie/stories", json={"text": "avatars"})
        assert r.status_code == 202
        started = await asyncio.wait_for(anext(changes), 5)
        ended = await asyncio.wait_for(anext(changes), 5)
        assert [(x.topic, x.key) for x in (started, ended)] == [("jobs", "story-1")] * 2
        assert ended.id == started.id + 1
