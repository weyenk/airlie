"""The board run dispatcher: an Approved card starts its workflow; one with none to run is Blocked.

No agent, engine, clock or sleep is involved: the start function is a mock and the clock a constant."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from airlie.board import BoardStore
from airlie.config.schema import RunsSpec
from airlie.serve.dispatcher import RunDispatcher


def make_dispatcher(board: BoardStore, start: AsyncMock) -> RunDispatcher:
    return RunDispatcher(
        board=board,
        cfg=SimpleNamespace(runs=RunsSpec(max_concurrent=2)),
        start=start,
        publish=lambda topic, key: None,
        clock=lambda: 0.0,
    )


async def test_approved_without_workflow_moves_to_blocked(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    start = AsyncMock()
    card = board.create(level="story", title="no workflow", labels=("area:web",))
    disp = make_dispatcher(board, start)

    board.move(card.key, "approved")
    await disp.notify_status(card.key, "approved")

    blocked = board.get(card.key)
    assert blocked.status == "blocked"
    assert [(m.to_status, m.by) for m in board.moves(card.key)][-1] == ("blocked", "dispatcher")
    assert [(c.author, c.text) for c in blocked.comments] == [("dispatcher", "missing workflow")]
    start.assert_not_awaited()
    board.close()


async def test_approved_with_workflow_starts_and_moves_doing_then_review(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    entered, release = asyncio.Event(), asyncio.Event()
    seen: list[str] = []
    published: list[tuple[str, str]] = []

    async def start(*, name: str, card: dict[str, Any], key: str) -> object:
        seen.append(card["status"])
        entered.set()
        await release.wait()
        return SimpleNamespace(ok=True)

    card = board.create(level="story", title="go", labels=("workflow:story",))
    disp = make_dispatcher(board, start)  # type: ignore[arg-type]
    disp.publish = lambda topic, key: published.append((topic, key))

    board.move(card.key, "approved")
    await disp.notify_status(card.key, "approved")
    await entered.wait()
    assert seen == ["doing"]
    assert board.get(card.key).status == "doing"

    release.set()
    await disp.drain()

    assert board.get(card.key).status == "review"
    assert [(m.to_status, m.by) for m in board.moves(card.key)][-2:] == [
        ("doing", "dispatcher"),
        ("review", "dispatcher"),
    ]
    assert published == [("board", card.key)] * 2
    board.close()


async def test_start_receives_workflow_name_and_card(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    start = AsyncMock(return_value=SimpleNamespace(ok=True))
    card = board.create(level="story", title="named", labels=("area:web", "workflow:layer"))
    disp = make_dispatcher(board, start)

    board.move(card.key, "approved")
    await disp.notify_status(card.key, "approved")
    await disp.drain()

    start.assert_awaited_once()
    kwargs = start.await_args.kwargs
    assert (kwargs["name"], kwargs["key"], kwargs["card"]["title"]) == ("layer", card.key, "named")
    board.close()


async def test_hard_fail_moves_to_blocked(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    start = AsyncMock(return_value=SimpleNamespace(ok=False, error="engine said no"))
    card = board.create(level="story", title="bad", labels=("workflow:story",))
    disp = make_dispatcher(board, start)
    published: list[tuple[str, str]] = []
    disp.publish = lambda topic, key: published.append((topic, key))

    board.move(card.key, "approved")
    await disp.notify_status(card.key, "approved")
    await disp.drain()

    blocked = board.get(card.key)
    assert blocked.status == "blocked"
    assert [(c.author, c.text) for c in blocked.comments] == [("dispatcher", "engine said no")]
    assert published[-1] == ("board", card.key)
    assert len(published) >= 2  # Doing, then Blocked
    board.close()


async def test_start_exception_moves_to_blocked_with_error(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    start = AsyncMock(side_effect=RuntimeError("boom"))
    card = board.create(level="story", title="raises", labels=("workflow:story",))
    disp = make_dispatcher(board, start)

    board.move(card.key, "approved")
    await disp.notify_status(card.key, "approved")
    await disp.drain()

    blocked = board.get(card.key)
    assert blocked.status == "blocked"
    assert [c.text for c in blocked.comments] == ["boom"]
    board.close()


async def test_repeat_notify_while_in_flight_starts_once(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(**_: object) -> object:
        entered.set()
        await release.wait()
        return SimpleNamespace(ok=True)

    start = AsyncMock(side_effect=run)
    card = board.create(level="story", title="twice", labels=("workflow:story",))
    disp = make_dispatcher(board, start)

    board.move(card.key, "approved")
    await disp.notify_status(card.key, "approved")
    await entered.wait()
    await disp.notify_status(card.key, "approved")
    release.set()
    await disp.drain()

    start.assert_awaited_once()
    assert board.get(card.key).status == "review"
    board.close()


async def test_concurrent_notify_starts_once(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    start = AsyncMock(return_value=SimpleNamespace(ok=True))
    card = board.create(level="story", title="race", labels=("workflow:story",))
    disp = make_dispatcher(board, start)

    board.move(card.key, "approved")
    await asyncio.gather(*(disp.notify_status(card.key, "approved") for _ in range(3)))
    await disp.drain()

    start.assert_awaited_once()
    assert [m.to_status for m in board.moves(card.key)].count("doing") == 1
    board.close()


def _gated_start(entered: dict[str, asyncio.Event], gates: dict[str, asyncio.Event], order: list[str]) -> Any:
    async def start(*, name: str, card: dict[str, Any], key: str) -> object:
        order.append(key)
        entered[key].set()
        await gates[key].wait()
        return SimpleNamespace(ok=True)

    return start


async def test_second_card_stays_approved_until_slot_frees(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    a = board.create(level="story", title="a", labels=("workflow:story",))
    b = board.create(level="story", title="b", labels=("workflow:story",))
    entered = {a.key: asyncio.Event(), b.key: asyncio.Event()}
    gates = {a.key: asyncio.Event(), b.key: asyncio.Event()}
    order: list[str] = []
    disp = make_dispatcher(board, _gated_start(entered, gates, order))  # type: ignore[arg-type]
    disp.cfg = SimpleNamespace(runs=RunsSpec(max_concurrent=1))

    for card in (a, b):
        board.move(card.key, "approved")
        await disp.notify_status(card.key, "approved")
    await entered[a.key].wait()
    assert board.get(a.key).status == "doing"
    assert board.get(b.key).status == "approved"
    assert order == [a.key]

    gates[a.key].set()
    await entered[b.key].wait()
    assert board.get(b.key).status == "doing"
    gates[b.key].set()
    await disp.drain()

    assert board.get(a.key).status == "review"
    assert board.get(b.key).status == "review"
    board.close()


async def test_fifo_order_when_slot_opens(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    cards = [board.create(level="story", title=t, labels=("workflow:story",)) for t in "xabc"]
    x, a, b, c = (card.key for card in cards)
    entered = {k: asyncio.Event() for k in (x, a, b, c)}
    gates = {k: asyncio.Event() for k in (x, a, b, c)}
    order: list[str] = []
    disp = make_dispatcher(board, _gated_start(entered, gates, order))  # type: ignore[arg-type]
    disp.cfg = SimpleNamespace(runs=RunsSpec(max_concurrent=1))

    for key in (x, a, b, c):
        board.move(key, "approved")
        await disp.notify_status(key, "approved")
    await entered[x].wait()
    for key in (x, a, b, c):
        gates[key].set()
    await disp.drain()

    assert order == [x, a, b, c]
    board.close()


async def test_busy_worktree_stays_approved_and_queued_without_blocking(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    busy = board.create(level="story", title="busy", labels=("workflow:story",))
    free = board.create(level="story", title="free", labels=("workflow:story",))
    start = AsyncMock(return_value=SimpleNamespace(ok=True))
    locked = {"busy": True}
    disp = make_dispatcher(board, start)
    disp.workdir_for = lambda card: Path(card["title"])
    disp.can_lock = lambda path: not (locked.get(path.name, False))

    for card in (busy, free):
        board.move(card.key, "approved")
        await disp.notify_status(card.key, "approved")
    await disp.drain()

    assert board.get(busy.key).status == "approved"
    assert board.get(busy.key).comments == ()
    assert board.get(free.key).status == "review"  # a busy card does not hold up the one behind it
    assert [c.kwargs["key"] for c in start.await_args_list] == [free.key]

    locked["busy"] = False
    await disp.pump()
    await disp.drain()

    assert board.get(busy.key).status == "review"
    board.close()


async def test_can_lock_defaults_to_free(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    start = AsyncMock(return_value=SimpleNamespace(ok=True))
    card = board.create(level="story", title="d", labels=("workflow:story",))
    disp = make_dispatcher(board, start)
    disp.workdir_for = lambda card: Path(card["title"])  # no can_lock injected

    board.move(card.key, "approved")
    await disp.notify_status(card.key, "approved")
    await disp.drain()

    assert board.get(card.key).status == "review"
    board.close()
