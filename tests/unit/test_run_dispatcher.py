"""The board run dispatcher: an Approved card starts its workflow; one with none to run is Blocked.

No agent, engine, clock or sleep is involved: the start function is a mock and the clock a constant."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
