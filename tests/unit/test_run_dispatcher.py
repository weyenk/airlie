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


class Gate:
    """A fake workflow engine: each started run waits on its own gate; pause/resume are recorded."""

    def __init__(self, keys: list[str]) -> None:
        self.entered = {k: asyncio.Event() for k in keys}
        self.gates = {k: asyncio.Event() for k in keys}
        self.order: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.pause = AsyncMock(side_effect=lambda key: self.calls.append(("pause", key)))
        self.resumed = asyncio.Event()
        self.resume = AsyncMock(side_effect=self._resume)

    def _resume(self, key: str) -> None:
        self.calls.append(("resume", key))
        self.resumed.set()

    async def start(self, *, name: str, card: dict[str, Any], key: str) -> object:
        self.order.append(key)
        self.entered[key].set()
        await self.gates[key].wait()
        return SimpleNamespace(ok=True)


def chat_dispatcher(board: BoardStore, gate: Gate, cap: int = 1) -> RunDispatcher:
    disp = make_dispatcher(board, gate.start)  # type: ignore[arg-type]
    disp.cfg = SimpleNamespace(runs=RunsSpec(max_concurrent=cap))
    disp.pause_run = gate.pause
    disp.resume_run = gate.resume
    return disp


async def approve(board: BoardStore, disp: RunDispatcher, *keys: str) -> None:
    for key in keys:
        board.move(key, "approved")
        await disp.notify_status(key, "approved")


async def test_chat_pauses_doing_board_run_then_resumes(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    a = board.create(level="story", title="a", labels=("workflow:story",))
    gate = Gate([a.key])
    disp = chat_dispatcher(board, gate)

    await approve(board, disp, a.key)
    await gate.entered[a.key].wait()
    lease = await disp.request_chat_slot("s1")

    gate.pause.assert_awaited_once_with(a.key)
    gate.resume.assert_not_awaited()
    assert board.get(a.key).status == "doing"  # never orphaned: still Doing, with a note
    assert [c.text for c in board.get(a.key).comments] == ["paused for chat"]

    await lease.release()

    gate.resume.assert_awaited_once_with(a.key)
    assert board.get(a.key).status == "doing"
    gate.gates[a.key].set()
    await disp.drain()
    assert board.get(a.key).status == "review"
    assert [c.text for c in board.get(a.key).comments] == ["paused for chat", "resumed after chat"]
    board.close()


async def test_chat_takes_a_free_slot_without_pausing(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    a = board.create(level="story", title="a", labels=("workflow:story",))
    gate = Gate([a.key])
    disp = chat_dispatcher(board, gate, cap=2)

    await approve(board, disp, a.key)
    await gate.entered[a.key].wait()
    async with await disp.request_chat_slot("s1"):
        gate.pause.assert_not_awaited()
    gate.resume.assert_not_awaited()
    gate.gates[a.key].set()
    await disp.drain()
    board.close()


async def test_chat_holds_slot_ahead_of_queue_and_paused_run_resumes_first(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    a = board.create(level="story", title="a", labels=("workflow:story",))
    b = board.create(level="story", title="b", labels=("workflow:story",))
    gate = Gate([a.key, b.key])
    disp = chat_dispatcher(board, gate)

    await approve(board, disp, a.key, b.key)
    await gate.entered[a.key].wait()
    lease = await disp.request_chat_slot("s1")
    await disp.pump()
    assert board.get(b.key).status == "approved"  # the queue waits for the chat

    await lease.release()
    assert gate.calls == [("pause", a.key), ("resume", a.key)]
    assert board.get(b.key).status == "approved"  # a, resumed, still holds the one slot

    gate.gates[a.key].set()
    await gate.entered[b.key].wait()  # starts once a finishes
    assert board.get(a.key).status == "review"
    gate.gates[b.key].set()
    await disp.drain()
    assert gate.order == [a.key, b.key]
    board.close()


async def test_chat_waiting_for_a_slot_goes_ahead_of_the_queue(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    a = board.create(level="story", title="a", labels=("workflow:story",))
    gate = Gate([a.key])
    disp = chat_dispatcher(board, gate)
    disp.pause_run = None  # nothing can be paused, so a chat must wait for the slot

    await approve(board, disp, a.key)
    await gate.entered[a.key].wait()
    waiting = asyncio.create_task(disp.request_chat_slot("s1"))
    await asyncio.sleep(0)
    assert not waiting.done()

    b = board.create(level="story", title="b", labels=("workflow:story",))
    gate.entered[b.key] = asyncio.Event()
    gate.gates[b.key] = asyncio.Event()
    await approve(board, disp, b.key)

    gate.gates[a.key].set()
    lease = await waiting
    assert lease.session_id == "s1"
    assert board.get(b.key).status == "approved"  # the chat got the freed slot, not b
    await lease.release()
    await gate.entered[b.key].wait()
    gate.gates[b.key].set()
    await disp.drain()
    board.close()


async def test_chat_pauses_board_run_rather_than_waiting_for_another_chat(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    a = board.create(level="story", title="a", labels=("workflow:story",))
    gate = Gate([a.key])
    disp = chat_dispatcher(board, gate, cap=2)

    first = await disp.request_chat_slot("s1")
    await approve(board, disp, a.key)
    await gate.entered[a.key].wait()
    second = await disp.request_chat_slot("s2")  # full: one chat and one board run

    gate.pause.assert_awaited_once_with(a.key)
    await first.release()
    gate.resume.assert_awaited_once_with(a.key)
    await second.release()
    gate.gates[a.key].set()
    await disp.drain()
    board.close()


async def test_chat_never_pauses_another_chat(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    gate = Gate([])
    disp = chat_dispatcher(board, gate)

    first = await disp.request_chat_slot("s1")
    waiting = asyncio.create_task(disp.request_chat_slot("s2"))
    await asyncio.sleep(0)
    assert not waiting.done()
    gate.pause.assert_not_awaited()

    await first.release()
    second = await waiting
    assert second.session_id == "s2"
    await second.release()
    await second.release()  # idempotent
    board.close()


async def test_pause_failure_leaves_run_doing_and_raises(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    a = board.create(level="story", title="a", labels=("workflow:story",))
    gate = Gate([a.key])
    disp = chat_dispatcher(board, gate)
    gate.pause.side_effect = RuntimeError("cannot pause")

    await approve(board, disp, a.key)
    await gate.entered[a.key].wait()
    try:
        await disp.request_chat_slot("s1")
    except RuntimeError as exc:
        assert str(exc) == "cannot pause"
    else:
        raise AssertionError("expected the pause error")

    assert board.get(a.key).status == "doing"
    assert board.get(a.key).comments == ()
    gate.gates[a.key].set()
    await disp.drain()
    assert board.get(a.key).status == "review"
    board.close()


async def test_paused_run_resumes_when_a_slot_frees_while_chat_still_holds_one(tmp_path: Path) -> None:
    board = BoardStore(tmp_path / "board.sqlite")
    a = board.create(level="story", title="a", labels=("workflow:story",))
    b = board.create(level="story", title="b", labels=("workflow:story",))
    gate = Gate([a.key, b.key])
    disp = chat_dispatcher(board, gate, cap=2)

    await approve(board, disp, a.key, b.key)
    await gate.entered[a.key].wait()
    await gate.entered[b.key].wait()
    lease = await disp.request_chat_slot("s1")  # pauses b, the newest
    assert gate.calls == [("pause", b.key)]

    gate.gates[a.key].set()  # a finishes; its slot lets b resume although the chat still holds one
    await gate.entered[a.key].wait()
    await gate.resumed.wait()
    assert ("resume", b.key) in gate.calls
    await lease.release()
    gate.gates[b.key].set()
    await disp.drain()
    assert board.get(b.key).status == "review"
    board.close()
