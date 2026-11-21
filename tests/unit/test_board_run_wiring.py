"""`task serve` wires the run dispatcher: the board routes tell it of an approval, chat takes a slot from it,
and the retired story line no longer ticks. Fakes only: no engine, agent, network or real clock."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from airlie.board import BoardStore
from airlie.board.api import board_router
from airlie.serve import ticker as ticker_mod
from airlie.serve.app import create_app
from airlie.serve.board_runs import BoardRuns, GatedEngine, card_workdir, lock_is_free
from airlie.serve.jobs import Jobs
from airlie.serve.ticker import QUIET, LineTicker
from airlie.story.model import RepositoryError
from airlie.workflow.worktree import acquire_lock, lock_path
from tests.support.serve import cfg
from tests.unit.serve_helpers import TestClient

# ---- the board routes tell the dispatcher ----


def board_client(store: BoardStore, notify: AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(board_router(store, notify=notify))
    return TestClient(app)


def test_a_move_and_a_change_to_approved_notify_the_dispatcher(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "b.sqlite")
    notify = AsyncMock()
    c = board_client(store, notify)
    one = c.post("/v1/board/items", json={"level": "initiative", "title": "a"}).json()["key"]
    two = c.post("/v1/board/items", json={"level": "initiative", "title": "b"}).json()["key"]
    notify.assert_not_awaited()  # a create is no status change

    assert c.post(f"/v1/board/items/{one}/move", json={"status": "approved"}).json()["status"] == "approved"
    assert c.patch(f"/v1/board/items/{two}", json={"status": "approved"}).json()["status"] == "approved"

    assert [call.args for call in notify.await_args_list] == [(one, "approved"), (two, "approved")]
    store.close()


def test_a_write_that_changes_no_status_or_is_refused_notifies_nobody(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "b.sqlite")
    notify = AsyncMock()
    c = board_client(store, notify)
    key = c.post("/v1/board/items", json={"level": "initiative", "title": "a"}).json()["key"]

    assert c.patch(f"/v1/board/items/{key}", json={"title": "b"}).status_code == 200
    assert c.post("/v1/board/items/AIR-99/move", json={"status": "approved"}).status_code == 404
    assert (
        c.post(f"/v1/board/items/{key}/move", json={"status": "doing", "before": "AIR-99"}).status_code == 422
    )

    notify.assert_not_awaited()
    store.close()


def test_a_board_router_with_no_dispatcher_still_moves_cards(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "b.sqlite")
    app = FastAPI()
    app.include_router(board_router(store))
    c = TestClient(app)
    key = c.post("/v1/board/items", json={"level": "initiative", "title": "a"}).json()["key"]
    assert c.post(f"/v1/board/items/{key}/move", json={"status": "approved"}).status_code == 200
    store.close()


# ---- the app: an approval through HTTP runs the workflow and lands the card in Review ----


async def test_approving_a_card_over_http_runs_its_workflow_to_review(tmp_path: Path) -> None:
    started: list[tuple[str, str]] = []

    async def run_start(*, name: str, card: dict[str, Any], key: str) -> object:
        started.append((name, card["status"]))
        return SimpleNamespace(ok=True)

    app = create_app(cfg(tmp_path), run_start=run_start)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        made = await c.post(
            "/v1/board/items", json={"level": "initiative", "title": "x", "labels": ["workflow:story"]}
        )
        key = made.json()["key"]
        moved = await c.post(f"/v1/board/items/{key}/move", json={"status": "approved"})
        assert moved.status_code == 200
        await app.state.dispatcher.drain()
        card = (await c.get(f"/v1/board/items/{key}")).json()

    assert started == [("story", "doing")]
    assert card["status"] == "review"


async def test_approving_a_card_with_no_workflow_over_http_blocks_it(tmp_path: Path) -> None:
    run_start = AsyncMock()
    app = create_app(cfg(tmp_path), run_start=run_start)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        key = (await c.post("/v1/board/items", json={"level": "initiative", "title": "x"})).json()["key"]
        await c.post(f"/v1/board/items/{key}/move", json={"status": "approved"})
        card = (await c.get(f"/v1/board/items/{key}")).json()

    assert card["status"] == "blocked"
    run_start.assert_not_awaited()


# ---- the retired story line ----


async def test_the_default_beat_does_not_tick_the_retired_story_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def retired(*_: Any, **__: Any) -> None:
        raise AssertionError("the story line's tick is retired")

    for name in ("tick", "story_config", "story_problems"):
        monkeypatch.setattr(ticker_mod.factory, name, retired, raising=False)
    pumped = AsyncMock()
    t = LineTicker(cfg(tmp_path), Jobs(lambda *_: None), commit=None, pump=pumped, now=lambda: 5.0)

    assert await t.tick() == QUIET
    assert (t.last_tick, t.last_line) == (5.0, QUIET)
    pumped.assert_awaited_once()


async def test_a_beat_with_no_dispatcher_is_quiet_and_an_injected_beat_still_runs(tmp_path: Path) -> None:
    quiet = LineTicker(cfg(tmp_path), Jobs(lambda *_: None), commit=None)
    assert await quiet.beat() == QUIET

    async def mine() -> str:
        return "mine"

    assert await LineTicker(cfg(tmp_path), Jobs(lambda *_: None), commit=None, beat=mine).beat() == "mine"


async def test_a_failing_pump_is_a_beat_that_failed_not_a_dead_ticker(tmp_path: Path) -> None:
    pump = AsyncMock(side_effect=RuntimeError("boom"))
    t = LineTicker(cfg(tmp_path), Jobs(lambda *_: None), commit=None, pump=pump)
    assert await t.beat() == "beat failed (RuntimeError: boom)"


# ---- the real start's pieces ----


def test_a_card_without_a_repo_label_runs_in_its_own_directory(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    where = card_workdir(c, {"key": "AIR-1", "labels": ["workflow:story"]})
    assert where == c.observability.state_dir.expanduser() / "runs" / "AIR-1" and where.is_dir()


def test_a_card_runs_in_its_repos_checkout_and_a_missing_one_is_refused_to_start(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.story.repos_root = tmp_path / "repos"
    (tmp_path / "repos" / "thing").mkdir(parents=True)
    card = {"key": "AIR-1", "labels": ["repo:acme/Thing"]}
    assert card_workdir(c, card) == tmp_path / "repos" / "thing"

    gone = {"key": "AIR-2", "labels": ["repo:acme/other"]}
    assert card_workdir(c, gone).name == "AIR-2"  # asked for a lock check only: lenient
    with pytest.raises(RepositoryError):
        card_workdir(c, gone, strict=True)


def test_can_lock_only_looks_at_the_lock_file(tmp_path: Path) -> None:
    assert lock_is_free(tmp_path)
    assert lock_is_free(tmp_path)  # looking took nothing
    assert not lock_path(tmp_path).exists()
    assert acquire_lock(tmp_path)
    assert not lock_is_free(tmp_path)


class FakeEngine:
    """Counts the model calls it serves."""

    def __init__(self) -> None:
        self.capabilities = SimpleNamespace()
        self.calls = 0
        self.started = self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def generate(self, req: Any, on_token: Any = None) -> str:
        self.calls += 1
        return "ok"

    async def yes_no(self, role: str, messages: Any) -> tuple[float, float]:
        self.calls += 1
        return 1.0, 0.0


async def test_start_runs_the_workflow_in_the_workdir_over_a_gated_engine(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    inner = FakeEngine()
    seen: dict[str, Any] = {}

    async def start_workflow(name: str, *, cfg: Any, engine: Any, workdir: Path, card: Any) -> object:
        seen.update(name=name, workdir=workdir, card=card, engine=engine)
        return SimpleNamespace(events=[])

    runs = BoardRuns(c, lambda: inner, start_workflow=start_workflow)  # type: ignore[arg-type]
    card = {"key": "AIR-1", "labels": [], "status": "doing"}
    out = await runs.start(name="story", card=card, key="AIR-1")

    assert out.ok and seen["name"] == "story" and seen["card"] is card
    assert isinstance(seen["engine"], GatedEngine) and seen["workdir"].name == "AIR-1"
    assert inner.started and inner.stopped and "AIR-1" not in runs.gates


async def test_a_workdir_another_run_holds_is_a_failed_start(tmp_path: Path) -> None:
    async def start_workflow(name: str, **_: Any) -> object:
        return SimpleNamespace(events=[{"kind": "already_running"}])

    runs = BoardRuns(cfg(tmp_path), FakeEngine, start_workflow=start_workflow)  # type: ignore[arg-type]
    out = await runs.start(name="story", card={"key": "AIR-1", "labels": []}, key="AIR-1")
    assert not out.ok and "already running" in (out.error or "")


async def test_a_failing_run_still_stops_its_engine_and_drops_its_gate(tmp_path: Path) -> None:
    inner = FakeEngine()

    async def start_workflow(name: str, **_: Any) -> object:
        raise RuntimeError("agent fell over")

    runs = BoardRuns(cfg(tmp_path), lambda: inner, start_workflow=start_workflow)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="agent fell over"):
        await runs.start(name="story", card={"key": "AIR-1", "labels": []}, key="AIR-1")
    assert inner.stopped and not runs.gates


async def test_pause_holds_the_runs_next_model_call_until_resume(tmp_path: Path) -> None:
    inner = FakeEngine()
    first, paused, second_asked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished = asyncio.Event()

    async def start_workflow(name: str, *, engine: Any, **_: Any) -> object:
        await engine.generate(None)
        first.set()
        await paused.wait()
        second_asked.set()
        await engine.generate(None)  # held while paused
        finished.set()
        return SimpleNamespace(events=[])

    runs = BoardRuns(cfg(tmp_path), lambda: inner, start_workflow=start_workflow)  # type: ignore[arg-type]
    task = asyncio.create_task(runs.start(name="story", card={"key": "AIR-1", "labels": []}, key="AIR-1"))
    await first.wait()
    await runs.pause_run("AIR-1")
    paused.set()
    await second_asked.wait()
    for _ in range(3):
        await asyncio.sleep(0)  # yields only: the paused call must not get through
    assert inner.calls == 1 and not finished.is_set()

    await runs.resume_run("AIR-1")
    assert (await task).ok and inner.calls == 2


async def test_pausing_or_resuming_a_run_that_is_not_running_is_a_no_op(tmp_path: Path) -> None:
    runs = BoardRuns(cfg(tmp_path), FakeEngine)
    await runs.pause_run("AIR-9")
    await runs.resume_run("AIR-9")


# ---- chat takes a slot ----


async def test_a_chat_turn_holds_a_dispatcher_slot_and_gives_it_back(tmp_path: Path) -> None:
    from tests.support.serve_chat import ScriptedUpstream, Text, chat_cfg

    slots: list[str] = []
    held_during: list[int] = []

    from airlie.serve import dispatcher as dispatcher_mod

    real: Callable[..., Any] = dispatcher_mod.RunDispatcher.request_chat_slot

    async def spy(self: Any, session_id: str) -> Any:
        slots.append(session_id)
        lease = await real(self, session_id)
        held_during.append(len(self._leases))
        return lease

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dispatcher_mod.RunDispatcher, "request_chat_slot", spy)
        app = create_app(chat_cfg(tmp_path), transport=httpx.MockTransport(ScriptedUpstream(Text("hi"))))
    body = {
        "id": "t1",
        "kind": "plain",
        "messages": [{"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}],
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/airlie/chat/turn", json=body)

    assert r.status_code == 200 and "hi" in r.text
    assert slots == ["t1"] and held_during == [1]
    assert app.state.dispatcher._leases == []  # released when the turn ended


def test_a_failing_dispatcher_does_not_fail_the_board_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = BoardStore(tmp_path / "b.sqlite")
    notify = AsyncMock(side_effect=RuntimeError("dispatcher down"))
    c = board_client(store, notify)
    key = c.post("/v1/board/items", json={"level": "initiative", "title": "a"}).json()["key"]

    moved = c.post(f"/v1/board/items/{key}/move", json={"status": "approved"})
    changed = c.patch(f"/v1/board/items/{key}", json={"status": "doing"})

    assert (moved.status_code, moved.json()["status"]) == (200, "approved")
    assert (changed.status_code, changed.json()["status"]) == (200, "doing")
    assert store.get(key).status == "doing"
    assert "the run dispatcher failed" in caplog.text
    store.close()


async def test_app_shutdown_ends_board_runs_before_the_board_closes(tmp_path: Path) -> None:
    entered = asyncio.Event()

    async def run_start(*, name: str, card: dict[str, Any], key: str) -> object:
        entered.set()
        await asyncio.Event().wait()
        return SimpleNamespace(ok=True)

    c = cfg(tmp_path)
    app = create_app(c, run_start=run_start)
    async with app.router.lifespan_context(app):  # its exit is serve's shutdown, board.close() included
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            made = await http.post(
                "/v1/board/items", json={"level": "initiative", "title": "x", "labels": ["workflow:story"]}
            )
            key = made.json()["key"]
            await http.post(f"/v1/board/items/{key}/move", json={"status": "approved"})
            await entered.wait()
        dispatcher = app.state.dispatcher

    assert dispatcher._inflight == {}
    reopened = BoardStore(c.observability.state_dir.expanduser() / "board.sqlite", prefix=c.board.prefix)
    assert reopened.get(key).status == "blocked"
    assert [m.text for m in reopened.get(key).comments] == ["interrupted: serve shut down"]
    reopened.close()


async def test_the_apps_transport_reaches_the_default_run_engine(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    from airlie.serve.board_runs import default_engine_factory

    c = cfg(tmp_path)
    engine = default_engine_factory(c, httpx.MockTransport(upstream))()
    await engine.start()
    await engine.health()
    await engine.stop()
    assert seen  # the mock transport served it: nothing went to the network
