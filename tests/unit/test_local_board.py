"""LocalBoard: the story pipeline over airlie's own board (story.board: local)."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.board import BoardStore
from airlie.story.board import NOTE_PREFIX, BoardError, Card, NullBoard, note
from airlie.story.epic_state import EpicState
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


async def test_a_note_without_a_role_is_stored_by_airlie_with_the_prefix(tmp_path: Path) -> None:
    """AC2: the no-role path is unchanged — a plain pipeline note is authored by airlie, prefix and all."""
    s, b = filled(tmp_path)
    card = await b.card("AIR-3")
    await b.comment(card, note("blocked: the gate failed"), role=None)
    [c] = s.get("AIR-3").comments
    assert c.author == "airlie" and c.text == NOTE_PREFIX + "blocked: the gate failed"


async def test_a_note_with_a_role_is_stored_by_that_role_but_keeps_the_prefix(tmp_path: Path) -> None:
    """The new seam: a note posted with a role is authored by that role, but the prefix still leads it."""
    s, b = filled(tmp_path)
    card = await b.card("AIR-3")
    await b.comment(card, note("the design is ready"), role="architect")
    [c] = s.get("AIR-3").comments
    assert c.author == "architect" and c.text == NOTE_PREFIX + "the design is ready"


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


async def test_the_card_omits_airlies_and_roles_notes_but_keeps_noahs(tmp_path: Path) -> None:
    """AC3: the card the pipeline reads drops airlie's and the configured roles' notes (by author, or by the
    **airlie** · prefix) and keeps a person's. The role note is seeded directly with no prefix, so only the
    author branch can drop it (note() would keep the prefix and hide that branch); noah's is present in the
    same card, so the omission and the inclusion are proven together."""
    s = BoardStore(tmp_path / "board.sqlite")
    init = s.create(level="initiative", title="i", description="init")
    epic = s.create(level="epic", title="e", parent=init.key, labels=["repo:weyenk/airlie"])
    s.create(level="story", title="first", description="one", parent=epic.key, labels=["repo:weyenk/airlie"])
    b = LocalBoard(s, role_names={"architect", "coder"})
    s.comment("AIR-3", "airlie", NOTE_PREFIX + "picked this up")  # airlie's note: prefix and author
    s.comment("AIR-3", "architect", "the design is settled")  # a configured role, no prefix: author branch
    s.comment("AIR-3", "noah", "please keep it small")  # a person, no prefix: stays
    card = await b.card("AIR-3")
    assert card.comments == ("please keep it small",)
    assert "the design is settled" not in card.comments  # the role's note is out of the next brief
    assert (NOTE_PREFIX + "picked this up") not in card.comments  # airlie's note is out of the next brief


# ---- epics: what the tick asks the board (P3, 2026-10-24) ----


def epic_board(tmp_path: Path) -> tuple[BoardStore, LocalBoard, Path]:
    s = BoardStore(tmp_path / "board.sqlite")
    epics = tmp_path / "epics"
    return s, LocalBoard(s, epics_dir=epics), epics


def line_story(s: BoardStore, epic: str, plan_id: str, *after: str, status: str = "approved") -> str:
    """A story the line made under `epic`: its labels, and `after` the keys it stacks on."""
    labels = ["repo:o/r", f"epic:{epic}", f"plan:{plan_id}", *(f"after:{k}" for k in after)]
    key = s.create(level="story", title=plan_id, parent=epic, labels=labels).key
    s.update(key, status=status)  # type: ignore[arg-type]
    return key


def test_next_epic_is_the_oldest_approved_epic_that_still_needs_its_stories(tmp_path: Path) -> None:
    s, b, epics = epic_board(tmp_path)
    one, two, three = (s.create(level="epic", title=t).key for t in ("one", "two", "three"))
    assert b.next_epic() is None  # nothing approved: an epic waits for a person
    s.update(two, status="approved")
    s.update(three, status="approved")
    first = b.next_epic()
    assert first is not None and first.key == two
    planned = EpicState.open(epics / two, s.get(two), "o/r")  # planned, and every story of it made
    planned.plan = {"stories": [{"id": "S1"}]}
    planned.stories = {"S1": "AIR-9"}
    planned.save()
    second = b.next_epic()
    assert second is not None and second.key == three
    planned.stories = {}  # a stop half-way through making the cards: it still needs work
    planned.save()
    again = b.next_epic()
    assert again is not None and again.key == two
    assert one == "AIR-1"  # the epic nobody approved was never offered


async def test_next_card_skips_a_story_of_an_epic_the_line_planned_while_a_person_holds_the_epic(
    tmp_path: Path,
) -> None:
    s, b, _ = epic_board(tmp_path)
    epic = s.create(level="epic", title="e").key
    mine = line_story(s, epic, "S1")
    hand = s.create(level="story", title="by hand", parent=epic).key  # a person's story under the same epic
    s.update(hand, status="approved")
    for held in ("todo", "blocked", "done"):
        s.update(epic, status=held)  # type: ignore[arg-type]
        found = await b.next_card()
        assert found is not None and found.key == hand, held  # the line's story waits; a person's does not
    for open_ in ("approved", "doing"):
        s.update(epic, status=open_)  # type: ignore[arg-type]
        found = await b.next_card()
        assert found is not None and found.key == mine, open_  # the older of the two


def test_waiting_stories_are_the_todo_dependents_of_an_epic_that_is_not_held(tmp_path: Path) -> None:
    s, b, _ = epic_board(tmp_path)
    epic = s.create(level="epic", title="e").key
    s.update(epic, status="approved")
    first = line_story(s, epic, "S1")
    second = line_story(s, epic, "S2", first, status="todo")
    line_story(s, epic, "S3", first, status="doing")  # started: not waiting
    s.create(level="story", title="a person's todo story", parent=epic)  # no after: label
    assert [i.key for i in b.waiting_stories()] == [second]
    b.set_epic_status(epic, "blocked")
    assert b.waiting_stories() == []  # a held epic promotes nothing
    b.set_epic_status(epic, "doing")
    assert [i.key for i in b.waiting_stories()] == [second]


def test_set_epic_status_moves_an_epic_and_refuses_anything_else(tmp_path: Path) -> None:
    s, b, _ = epic_board(tmp_path)
    epic = s.create(level="epic", title="e").key
    story = s.create(level="story", title="s", parent=epic).key
    b.set_epic_status(epic, "approved")
    assert s.get(epic).status == "approved"
    with pytest.raises(BoardError, match="is a story"):
        b.set_epic_status(story, "approved")
    with pytest.raises(BoardError, match="no item AIR-99"):
        b.set_epic_status("AIR-99", "approved")


def test_an_epics_status_follows_the_stories_the_line_made_for_it(tmp_path: Path) -> None:
    s, b, _ = epic_board(tmp_path)
    epic = s.create(level="epic", title="e").key
    s.update(epic, status="approved")
    a = line_story(s, epic, "S1")
    c = line_story(s, epic, "S2", a, status="todo")
    assert b.follow_stories() == []  # nothing picked yet: the epic stays approved
    s.update(a, status="doing")
    assert b.follow_stories() == [(epic, "approved", "doing")]
    s.update(a, status="review")
    assert b.follow_stories() == []  # a dependent still waits: the epic is not in review
    s.update(c, status="review")
    assert b.follow_stories() == [(epic, "doing", "review")]
    s.update(c, status="approved")  # a re-plan adds work: back to doing
    assert b.follow_stories() == [(epic, "review", "doing")]
    s.update(c, status="done")
    s.update(a, status="done")
    assert b.follow_stories() == [(epic, "doing", "done")]
    assert b.follow_stories() == []  # a finished epic is left alone
    # a person's hold is never overridden, and an epic with no story of the line's is not followed
    held = s.create(level="epic", title="held").key
    s.update(held, status="blocked")
    line_story(s, held, "S1", status="doing")
    plain = s.create(level="epic", title="plain").key
    s.update(plain, status="approved")
    s.update(s.create(level="story", title="x", parent=plain).key, status="doing")
    assert b.follow_stories() == []
