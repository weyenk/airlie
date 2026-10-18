"""LocalBoard: the story pipeline over airlie's own board (story.board: local)."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.board import BoardStore
from airlie.story.board import NOTE_PREFIX, BoardError, Card, NullBoard, note
from airlie.story.factory import _note_and_move, make_board
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
    s.comment("AIR-3", "noah", "please keep it small")
    card = await b.card("AIR-3")
    assert card == Card(
        key="AIR-3",
        id="AIR-3",
        ref="AIR-3",
        title="first",
        description="one",
        url=None,
        comments=("please keep it small",),
        labels=("repo:weyenk/airlie",),
    )
    assert await b.card("  AIR-3 ") == card


async def test_card_of_an_epic_or_an_unknown_key_is_a_board_error(tmp_path: Path) -> None:
    _, b = filled(tmp_path)
    with pytest.raises(BoardError, match="AIR-2 is an epic; only stories are built"):
        await b.card("AIR-2")
    with pytest.raises(BoardError, match="AIR-1 is an initiative"):
        await b.card("AIR-1")
    with pytest.raises(BoardError, match="AIR-99"):
        await b.card("AIR-99")
    with pytest.raises(BoardError, match="not a board item key"):
        await b.card("lowercase-1")


async def test_next_card_is_the_oldest_approved_story_never_a_todo_epic_or_initiative(tmp_path: Path) -> None:
    s, b = filled(tmp_path)
    assert await b.next_card() is None  # nothing is approved: todo stories wait for a person
    s.update("AIR-1", status="approved")
    s.update("AIR-2", status="approved")
    assert await b.next_card() is None  # an initiative and an epic are never built
    s.update("AIR-4", status="approved")
    s.update("AIR-3", status="approved")
    first = await b.next_card()
    assert first is not None and first.key == "AIR-3"
    s.update("AIR-3", status="doing")
    second = await b.next_card()
    assert second is not None and second.key == "AIR-4"
    s.update("AIR-4", status="done")
    assert await b.next_card() is None


async def test_move_maps_every_column_onto_a_status(tmp_path: Path) -> None:
    s, b = filled(tmp_path)
    card = await b.card("AIR-3")
    for column, status in (
        ("building", "doing"),
        ("review", "review"),
        ("blocked", "blocked"),
        ("intake", "todo"),
    ):
        await b.move(card, column)  # type: ignore[arg-type]
        assert s.get("AIR-3").status == status
    with pytest.raises(BoardError, match="AIR-99"):
        await b.move(Card("AIR-99", "AIR-99", "AIR-99", "gone", ""), "review")


async def test_comment_stores_the_pipelines_note_as_given_by_airlie(tmp_path: Path) -> None:
    """The pipeline passes note(text) already (as to every board): the prefix is not added twice."""
    s, b = filled(tmp_path)
    card = await b.card("AIR-3")
    await b.comment(card, note("blocked: the gate failed"))
    [c] = s.get("AIR-3").comments
    assert c.author == "airlie" and c.text == NOTE_PREFIX + "blocked: the gate failed"


async def test_a_note_then_a_move_lands_once_and_moves_the_item(tmp_path: Path) -> None:
    s, b = filled(tmp_path)
    card = await b.card("AIR-3")
    told = await _note_and_move(b, card, "the gate failed", "blocked")
    assert told == "noted on the issue, moved to blocked"
    item = s.get("AIR-3")
    assert item.status == "blocked" and [c.text for c in item.comments] == [NOTE_PREFIX + "the gate failed"]


async def test_airlies_own_notes_stay_out_of_the_card(tmp_path: Path) -> None:
    """As JiraBoard does: the next brief gets what people wrote, not airlie's status notes."""
    s, b = filled(tmp_path)
    s.comment("AIR-3", "noah", "keep it small")
    card = await b.card("AIR-3")
    await b.comment(card, note("picked this up"))
    s.comment("AIR-3", "noah", "and fast")
    assert (await b.card("AIR-3")).comments == ("keep it small", "and fast")


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
            {**MINIMAL, "observability": {"state_dir": str(state)}, "board": {"prefix": "HDS"}},
        )
    )
    board = make_board(local)  # local is the default
    assert isinstance(board, LocalBoard)
    assert board.store.path == state / "board.sqlite" and board.store.prefix == "HDS"
    board.store.close()
    jira = load_config(
        write(tmp_path, {**MINIMAL, "observability": {"state_dir": str(state)}, "story": {"board": "jira"}})
    )
    assert isinstance(make_board(jira), NullBoard)  # story.board: jira, but no Jira settings


async def test_status_of_names_the_column_a_card_is_in_now(tmp_path: Path) -> None:
    s, b = filled(tmp_path)
    card = await b.card("AIR-3")
    assert b.status_of(card) == "todo"
    s.move("AIR-3", "approved")
    assert b.status_of(card) == "approved"
