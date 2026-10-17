"""LocalBoard: the story pipeline over airlie's own board (story.board: local)."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.board import BoardStore
from airlie.story.board import NOTE_PREFIX, BoardError, Card, NullBoard
from airlie.story.factory import make_board
from airlie.story.local_board import LocalBoard
from tests.unit.test_config import MINIMAL, write


def filled(tmp_path: Path) -> tuple[BoardStore, LocalBoard]:
    s = BoardStore(tmp_path / "board.sqlite")
    init = s.create(level="initiative", title="i", description="init")
    epic = s.create(level="epic", title="e", parent=init.key, labels=["repo:weyenk/airlie"])
    s.create(level="story", title="first", description="one", parent=epic.key, labels=["repo:weyenk/airlie"])
    s.create(level="story", title="second", description="two", parent=epic.key)
    return s, LocalBoard(s)


async def test_card_is_a_story_with_its_text_comments_and_labels(tmp_path: Path) -> None:
    s, b = filled(tmp_path)
    s.comment("AL-3", "noah", "please keep it small")
    card = await b.card("AL-3")
    assert card == Card(
        key="AL-3",
        id="AL-3",
        ref="AL-3",
        title="first",
        description="one",
        url=None,
        comments=("please keep it small",),
        labels=("repo:weyenk/airlie",),
    )
    assert await b.card("  AL-3 ") == card


async def test_card_of_an_epic_or_an_unknown_key_is_a_board_error(tmp_path: Path) -> None:
    _, b = filled(tmp_path)
    with pytest.raises(BoardError, match="AL-2 is an epic; only stories are built"):
        await b.card("AL-2")
    with pytest.raises(BoardError, match="AL-1 is an initiative"):
        await b.card("AL-1")
    with pytest.raises(BoardError, match="AL-99"):
        await b.card("AL-99")
    with pytest.raises(BoardError, match="not a board item key"):
        await b.card("lowercase-1")


async def test_next_card_is_the_oldest_story_in_todo_never_an_epic_or_initiative(tmp_path: Path) -> None:
    s, b = filled(tmp_path)
    first = await b.next_card()
    assert first is not None and first.key == "AL-3"
    s.update("AL-3", status="doing")
    second = await b.next_card()
    assert second is not None and second.key == "AL-4"
    s.update("AL-4", status="done")
    assert await b.next_card() is None  # AL-1 and AL-2 are still todo


async def test_move_maps_every_column_onto_a_status(tmp_path: Path) -> None:
    s, b = filled(tmp_path)
    card = await b.card("AL-3")
    for column, status in (
        ("building", "doing"),
        ("review", "review"),
        ("blocked", "blocked"),
        ("intake", "todo"),
    ):
        await b.move(card, column)  # type: ignore[arg-type]
        assert s.get("AL-3").status == status
    with pytest.raises(BoardError, match="AL-99"):
        await b.move(Card("AL-99", "AL-99", "AL-99", "gone", ""), "review")


async def test_comment_is_written_by_airlie_with_its_prefix(tmp_path: Path) -> None:
    s, b = filled(tmp_path)
    card = await b.card("AL-3")
    await b.comment(card, "blocked: the gate failed")
    [c] = s.get("AL-3").comments
    assert c.author == "airlie" and c.text == NOTE_PREFIX + "blocked: the gate failed"
    assert (await b.card("AL-3")).comments == (NOTE_PREFIX + "blocked: the gate failed",)


def test_make_board_picks_the_local_board_from_story_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ATLASSIAN_BASE_URL", "ATLASSIAN_PROJECT_KEY", "ATLASSIAN_EMAIL", "ATLASSIAN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    from airlie.config import load_config

    state = tmp_path / "state"
    local = load_config(
        write(
            tmp_path,
            {
                **MINIMAL,
                "observability": {"state_dir": str(state)},
                "story": {"board": "local"},
                "board": {"prefix": "HDS"},
            },
        )
    )
    board = make_board(local)
    assert isinstance(board, LocalBoard)
    assert board.store.path == state / "board.sqlite" and board.store.prefix == "HDS"
    board.store.close()
    jira = load_config(write(tmp_path, {**MINIMAL, "observability": {"state_dir": str(state)}}))
    assert isinstance(make_board(jira), NullBoard)
