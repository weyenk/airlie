"""The watcher: what other processes write (the launchd story tick, `task run`, `task board:import-jira`,
`task auth:invite`) and this server's own writes, noticed by polling the state directory and published to the
live feed as topic hints. `poll()` is tested directly; the loop with a short interval and bounded waits."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.auth.store import AuthStore
from airlie.board import BoardStore
from airlie.serve.app import create_app
from airlie.serve.feed import Feed
from airlie.serve.watch import Watcher
from tests.unit.test_serve import FakeUpstream, cfg


@pytest.fixture
def state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


def append_run_log(state: Path, run_id: str, line: str = '{"kind": "step"}\n') -> None:
    d = state / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    with (d / "events.jsonl").open("a") as f:
        f.write(line)


def write_story(state: Path, key: str, stage: str) -> Path:
    f = state / "stories" / key / "state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"stage": stage}))
    return f


@pytest.fixture
def own_board(state: Path) -> Iterator[BoardStore]:
    """What `airlie serve` has after its first board request: the database exists, on this server's own
    connection."""
    own = BoardStore(state / "board.sqlite")
    own.list()  # opens it: the file and its schema
    yield own
    own.close()


def board_commit(state: Path, title: str = "x") -> None:
    """A write from another process (`task board:import-jira`, the story tick): its own connection."""
    other = BoardStore(state / "board.sqlite")
    other.create(level="initiative", title=title)
    other.close()


def auth_write(state: Path, sql: str) -> None:
    """A write from another process (`task auth:invite`): a plain connection to the file `AuthStore` made."""
    other = sqlite3.connect(state / "auth.sqlite", isolation_level=None)
    other.execute(sql)
    other.close()


ADA = "insert into users (id, name, role, created_at) values ('u1', 'Ada', 'member', 1.0)"


@pytest.fixture
def make_watcher(state: Path) -> Iterator[Callable[..., Watcher]]:
    """A watcher on `state` that is stopped when the test ends, whatever it asserted."""
    made: list[Watcher] = []

    def make(feed: Feed, kind: type[Watcher] = Watcher, **kw: Any) -> Watcher:
        w = kind(state, feed, **kw)
        made.append(w)
        return w

    yield make
    for w in made:
        w.stop()


async def eventually(done: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not done():
            await asyncio.sleep(0.005)


# ---- poll ----------------------------------------------------------------------------------------


def test_the_first_poll_publishes_nothing(state: Path, own_board: BoardStore) -> None:
    append_run_log(state, "r1")
    write_story(state, "AIR-7", "coding")
    assert Watcher(state, Feed()).poll() == []


def test_an_empty_state_dir_is_quiet_and_nothing_is_created(state: Path) -> None:
    w = Watcher(state / "never-made", Feed())
    assert w.poll() == [] and w.poll() == []
    assert not (state / "never-made").exists()


def test_two_board_commits_from_another_connection_give_one_board_event(
    state: Path, own_board: BoardStore
) -> None:
    w = Watcher(state, Feed())
    w.poll()
    other = BoardStore(state / "board.sqlite")
    other.create(level="initiative", title="one")
    other.create(level="initiative", title="two")
    other.close()
    assert w.poll() == [("board", None)]
    assert w.poll() == []
    own_board.create(level="initiative", title="three")  # this server's own write is noticed the same way
    assert w.poll() == [("board", None)]
    w.stop()


def test_auth_writes_publish_users(state: Path) -> None:
    AuthStore(state / "auth.sqlite").close()
    w = Watcher(state, Feed())
    w.poll()
    auth_write(state, ADA)
    assert w.poll() == [("users", None)]
    assert w.poll() == []
    auth_write(state, "update users set name = 'Ada L' where id = 'u1'")
    assert w.poll() == [("users", None)]
    w.stop()


def test_a_database_that_does_not_exist_yet_is_skipped_not_created_and_noticed_when_it_appears(
    state: Path,
) -> None:
    w = Watcher(state, Feed())
    assert w.poll() == [] and w.poll() == []
    assert not (state / "board.sqlite").exists() and not (state / "auth.sqlite").exists()
    board_commit(state)
    assert w.poll() == [("board", None)]  # it came into being after the starting point: a change
    assert w.poll() == []
    w.stop()


def test_the_watcher_never_writes_to_a_database(state: Path, own_board: BoardStore) -> None:
    AuthStore(state / "auth.sqlite").close()
    names = ("board.sqlite", "auth.sqlite")
    before = {n: (state / n).read_bytes() for n in names}
    w = Watcher(state, Feed())
    w.poll()
    w.poll()
    w.stop()
    assert {n: (state / n).read_bytes() for n in names} == before


def test_a_database_removed_and_made_again_is_read_afresh(state: Path) -> None:
    board_commit(state)
    w = Watcher(state, Feed())
    w.poll()
    for p in state.glob("board.sqlite*"):
        p.unlink()
    assert w.poll() == []  # gone: nothing to say
    board_commit(state)
    assert w.poll() == [("board", None)]
    w.stop()


def test_a_growing_run_log_publishes_its_run(state: Path) -> None:
    append_run_log(state, "r1")
    w = Watcher(state, Feed())
    w.poll()
    assert w.poll() == []
    append_run_log(state, "r1")
    assert w.poll() == [("runs", "r1")]
    assert w.poll() == []


def test_a_new_run_publishes_whether_or_not_it_has_a_log_yet(state: Path) -> None:
    append_run_log(state, "r1")
    w = Watcher(state, Feed())
    w.poll()
    (state / "runs" / "r2").mkdir()  # `task run` has made its directory and not yet its event log
    assert w.poll() == [("runs", "r2")]
    append_run_log(state, "r2")
    assert w.poll() == [("runs", "r2")]
    (state / "runs" / "stray.txt").write_text("not a run")  # a file is not a run
    assert w.poll() == []


def test_only_the_newest_runs_are_watched_and_one_sliding_out_is_not_a_change(state: Path) -> None:
    for run_id in ("r1", "r2", "r3"):
        append_run_log(state, run_id)
    w = Watcher(state, Feed(), recent_runs=2)  # r2 and r3: ids sort by time, the newest are the largest
    w.poll()
    append_run_log(state, "r1")  # outside the window
    assert w.poll() == []
    append_run_log(state, "r4")  # r2 slides out; r4 comes in
    assert w.poll() == [("runs", "r4")]
    append_run_log(state, "r2")
    assert w.poll() == []
    append_run_log(state, "r3")
    assert w.poll() == [("runs", "r3")]


def test_a_story_state_change_publishes_its_story(state: Path) -> None:
    write_story(state, "AIR-7", "coding")
    w = Watcher(state, Feed())
    w.poll()
    write_story(state, "AIR-7", "in review")
    assert w.poll() == [("stories", "AIR-7")]
    assert w.poll() == []


def test_a_state_json_rewritten_to_the_same_size_is_noticed_by_its_mtime(state: Path) -> None:
    f = write_story(state, "AIR-7", "coding")
    w = Watcher(state, Feed())
    w.poll()
    st = f.stat()
    write_story(state, "AIR-7", "gnidoc")  # the same length
    later = st.st_mtime_ns + 5_000_000_000  # as a filesystem with a coarse clock may leave it
    os.utime(f, ns=(st.st_atime_ns, later))
    assert f.stat().st_size == st.st_size
    assert w.poll() == [("stories", "AIR-7")]


def test_a_story_event_log_growing_publishes_its_story_and_a_new_story_directory_does_too(
    state: Path,
) -> None:
    write_story(state, "AIR-7", "coding")
    w = Watcher(state, Feed())
    w.poll()
    log = state / "stories" / "AIR-7" / "events.jsonl"
    log.write_text('{"kind": "step"}\n')
    assert w.poll() == [("stories", "AIR-7")]
    with log.open("a") as f:
        f.write('{"kind": "step"}\n')
    assert w.poll() == [("stories", "AIR-7")]
    (state / "stories" / "AIR-8").mkdir()  # the tick has made the directory and nothing in it
    assert w.poll() == [("stories", "AIR-8")]
    assert w.poll() == []


def test_several_changes_in_one_poll_come_back_once_each_in_a_fixed_order(
    state: Path, own_board: BoardStore
) -> None:
    AuthStore(state / "auth.sqlite").close()
    write_story(state, "AIR-7", "coding")
    write_story(state, "AIR-8", "coding")
    append_run_log(state, "r1")
    w = Watcher(state, Feed())
    w.poll()
    board_commit(state)
    auth_write(state, ADA)
    (state / "stories" / "AIR-8" / "events.jsonl").write_text("x\n")
    (state / "stories" / "AIR-7" / "events.jsonl").write_text("x\n")
    write_story(state, "AIR-7", "in review")  # AIR-7's log and its state both changed: still one hint
    append_run_log(state, "r1")
    assert w.poll() == [
        ("board", None),
        ("users", None),
        ("stories", "AIR-7"),
        ("stories", "AIR-8"),
        ("runs", "r1"),
    ]
    w.stop()


async def test_poll_publishes_to_the_feed_what_it_returns(state: Path) -> None:
    feed = Feed()
    append_run_log(state, "r1")
    w = Watcher(state, feed)
    w.poll()
    async with feed.subscribe() as sub:
        append_run_log(state, "r1")
        write_story(state, "AIR-7", "coding")
        returned = w.poll()
        it = aiter(sub)
        seen = [await asyncio.wait_for(anext(it), 5) for _ in returned]
    assert returned == [("stories", "AIR-7"), ("runs", "r1")]
    assert [(c.topic, c.key) for c in seen] == returned


# ---- the loop ------------------------------------------------------------------------------------


async def test_ensure_running_takes_its_starting_point_before_it_returns(
    state: Path, make_watcher: Callable[..., Watcher]
) -> None:
    """A write after ensure_running returns, which the stream does before it sends `: connected`, is never
    folded into the baseline: the loop's first poll is an hour away, and the poll made here sees the write."""
    feed = Feed()
    append_run_log(state, "r1")
    w = make_watcher(feed)
    async with feed.subscribe():
        w.ensure_running(interval_s=3600)
        assert w.running
        append_run_log(state, "r1")
        assert w.poll() == [("runs", "r1")]


async def test_ensure_running_publishes_nothing_for_what_was_already_there(
    state: Path, make_watcher: Callable[..., Watcher]
) -> None:
    feed = Feed()
    append_run_log(state, "r1")
    board_commit(state, "before")
    w = make_watcher(feed)
    async with feed.subscribe() as sub:
        w.ensure_running(interval_s=0.01)
        feed.publish("marker")
        change = await asyncio.wait_for(anext(aiter(sub)), 5)
        assert change.topic == "marker"  # nothing the watcher published came before it


async def test_the_loop_publishes_writes_while_a_page_is_connected_and_stops_when_none_is(
    state: Path, make_watcher: Callable[..., Watcher]
) -> None:
    feed = Feed()
    append_run_log(state, "r1")
    w = make_watcher(feed)
    async with feed.subscribe() as sub:
        w.ensure_running(interval_s=0.01)
        append_run_log(state, "r1")
        change = await asyncio.wait_for(anext(aiter(sub)), 5)
        assert (change.topic, change.key) == ("runs", "r1")
        assert w.running
    await eventually(lambda: not w.running)  # the page left: the count fell to 0


async def test_a_loop_that_ended_leaves_no_baseline_and_a_later_connection_starts_a_new_one(
    state: Path, make_watcher: Callable[..., Watcher]
) -> None:
    feed = Feed()
    append_run_log(state, "r1")
    w = make_watcher(feed)
    async with feed.subscribe():
        w.ensure_running(interval_s=0.01)
    await eventually(lambda: not w.running)
    append_run_log(state, "r1")  # written while nobody was listening
    async with feed.subscribe() as sub:
        w.ensure_running(interval_s=0.01)  # a fresh baseline: that write is in it
        assert w.running
        append_run_log(state, "r2")
        change = await asyncio.wait_for(anext(aiter(sub)), 5)
        assert (change.topic, change.key) == ("runs", "r2")  # not ("runs", "r1")
    await eventually(lambda: not w.running)


async def test_ensure_running_while_running_starts_no_second_loop_and_polls_nothing(
    state: Path, make_watcher: Callable[..., Watcher]
) -> None:
    polls = 0

    class Counting(Watcher):
        def poll(self) -> list[tuple[str, str | None]]:
            nonlocal polls
            polls += 1
            return super().poll()

    feed = Feed()
    w = make_watcher(feed, Counting)
    async with feed.subscribe():
        w.ensure_running(interval_s=3600)
        task = w._task
        w.ensure_running(interval_s=3600)
        w.ensure_running()
        assert polls == 1 and w._task is task


async def test_the_loop_survives_a_poll_that_fails_and_says_so(
    state: Path, make_watcher: Callable[..., Watcher], caplog: pytest.LogCaptureFixture
) -> None:
    class Flaky(Watcher):
        broken = False

        def poll(self) -> list[tuple[str, str | None]]:
            if self.broken:
                self.broken = False
                raise RuntimeError("disk on fire")
            return super().poll()

    feed = Feed()
    append_run_log(state, "r1")
    w = make_watcher(feed, Flaky)
    caplog.set_level(logging.ERROR, logger="airlie.serve.watch")
    async with feed.subscribe() as sub:
        w.ensure_running(interval_s=0.01)
        w.broken = True  # type: ignore[attr-defined]
        await eventually(lambda: "disk on fire" in caplog.text)
        append_run_log(state, "r1")
        change = await asyncio.wait_for(anext(aiter(sub)), 5)  # still polling
        assert (change.topic, change.key) == ("runs", "r1")


async def test_a_first_poll_that_fails_is_logged_and_the_loop_starts_and_takes_the_baseline_next(
    state: Path, make_watcher: Callable[..., Watcher], caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable state directory at connect time must not end the stream that asked for the watcher
    (its headers are already sent): ensure_running logs it, the loop starts, and its next poll records the
    starting point, so a write after that is a change."""

    class FirstFails(Watcher):
        polls = 0

        def poll(self) -> list[tuple[str, str | None]]:
            self.polls += 1
            if self.polls == 1:
                raise PermissionError("state dir unreadable")
            return super().poll()

    feed = Feed()
    append_run_log(state, "r1")
    w = make_watcher(feed, FirstFails)
    caplog.set_level(logging.WARNING, logger="airlie.serve.watch")
    async with feed.subscribe() as sub:
        w.ensure_running(interval_s=0.01)  # must not raise
        assert w.running
        [record] = [r for r in caplog.records if r.name == "airlie.serve.watch"]
        assert record.levelno == logging.WARNING and "state dir unreadable" in caplog.text
        await eventually(lambda: w.polls >= 2)  # the next poll recorded the baseline: nothing published
        append_run_log(state, "r1")
        change = await asyncio.wait_for(anext(aiter(sub)), 5)
        assert (change.topic, change.key) == ("runs", "r1")


async def test_stop_ends_the_loop_and_forgets_the_baseline(
    state: Path, make_watcher: Callable[..., Watcher]
) -> None:
    feed = Feed()
    append_run_log(state, "r1")
    w = make_watcher(feed)
    async with feed.subscribe():
        w.ensure_running(interval_s=3600)
        w.stop()
        assert not w.running
        append_run_log(state, "r1")
        assert w.poll() == []  # stopped: the next poll starts over
        w.stop()  # and stopping again is harmless


async def test_a_stopped_watcher_holds_no_database_connection(
    state: Path, make_watcher: Callable[..., Watcher]
) -> None:
    """Each database is held open only while pages are connected."""
    board_commit(state)
    feed = Feed()
    w = make_watcher(feed)
    async with feed.subscribe():
        w.ensure_running(interval_s=3600)
        assert w._dbs["board"]._conn is not None
        w.stop()
    assert all(db._conn is None for db in w._dbs.values())
    async with feed.subscribe():
        w.ensure_running(interval_s=3600)  # a later page: opened again
        assert w._dbs["board"]._conn is not None
    # and when the loop ends by itself (no pages), they are closed too
    w.stop()


async def test_a_loop_that_ends_by_itself_closes_the_connections_too(
    state: Path, make_watcher: Callable[..., Watcher]
) -> None:
    board_commit(state)
    feed = Feed()
    w = make_watcher(feed)
    async with feed.subscribe():
        w.ensure_running(interval_s=0.01)
        assert w._dbs["board"]._conn is not None
    await eventually(lambda: not w.running)
    assert all(db._conn is None for db in w._dbs.values())


# ---- the route -----------------------------------------------------------------------------------


async def test_the_events_route_has_the_watcher_running_and_its_baseline_taken_before_connected(
    tmp_path: Path,
) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    state = tmp_path / "state"
    feed: Feed = app.state.feed
    watcher: Watcher = app.state.watcher
    append_run_log(state, "r1")  # there before the page connects: part of the baseline
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
    try:
        assert (await asyncio.wait_for(sent.get(), 5))["status"] == 200
        assert (await asyncio.wait_for(sent.get(), 5))["body"] == b": connected\n\n"
        assert feed.subscribers == 1 and watcher.running
        append_run_log(state, "r1")  # after `: connected`: seen, by the poll made here as by the loop
        assert watcher.poll() == [("runs", "r1")]
        frame = (await asyncio.wait_for(sent.get(), 5))["body"]
        assert frame.endswith(b'data: {"topic": "runs", "key": "r1"}\n\n')
    finally:
        inbox.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(call, 5)
        watcher.stop()
    assert feed.subscribers == 0
