"""Layer 5 of the role-author story: each studio role posts its own notes as itself.

The board seam (LocalBoard/BoardStore) already stores a comment by the role passed to
``comment`` (else "airlie") and keeps the ``**airlie** · `` prefix. This layer makes the
*pipeline* pass the role that wrote each note, so a role's note is stored with that role as
the comment author (AC1) while still carrying the prefix.

These tests drive a real story on ``FakeEngine`` against a real ``BoardStore`` (via
``LocalBoard``) far enough to post each role-written note, then read the stored comment back
and assert its author is the role (not "airlie") and its text still begins with the prefix.
"""

from __future__ import annotations

from pathlib import Path

from airlie.board import BoardStore
from airlie.story.board import NOTE_PREFIX, Card
from airlie.story.local_board import LocalBoard
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_pipeline import happy_engine, pipeline


def _local_board(tmp_path: Path, role_names: set[str]) -> tuple[LocalBoard, Card]:
    """A real local board (SQLite) with one story item on it and the card for it."""
    store = BoardStore(tmp_path / "board.sqlite", prefix="AIR")
    item = store.create(level="story", title="Add avatar upload", description="Users upload an avatar.")
    card = Card(key=item.key, id=item.key, ref=item.key, title=item.title, description=item.description)
    return LocalBoard(store, role_names=role_names), card


async def test_a_refine_note_is_stored_by_the_product_role_not_airlie(tmp_path: Path) -> None:
    """AC1: the refine/story note is product's work, so it is stored with author "product" (not
    "airlie") while its text still begins with the ``**airlie** · `` prefix."""
    board, card = _local_board(tmp_path, {"product", "architect"})
    repo = repo_with_marker_tests(tmp_path)
    st = await pipeline(
        tmp_path,
        repo,
        happy_engine(),
        board=board,
        story={"product_role": "product", "architect_role": "architect"},
    ).run(card)
    assert st.blocked is None and st.stage == "review"  # the whole story ran, so the note was posted
    item = board.store.get(card.key)
    story_notes = [c for c in item.comments if c.text.startswith(NOTE_PREFIX + "story:")]
    assert len(story_notes) == 1  # exactly the refine/story note
    [story_note] = story_notes
    assert story_note.author == "product"  # the role that wrote it, not airlie
    assert story_note.author != "airlie"
    assert story_note.text.startswith(NOTE_PREFIX)  # still recognisable as airlie's


async def test_a_layers_note_is_stored_by_the_architect_role_not_airlie(tmp_path: Path) -> None:
    """AC1: the layers note is architect's work, so it is stored with author "architect" (not
    "airlie") while its text still begins with the ``**airlie** · `` prefix."""
    board, card = _local_board(tmp_path, {"product", "architect"})
    repo = repo_with_marker_tests(tmp_path)
    st = await pipeline(
        tmp_path,
        repo,
        happy_engine(),
        board=board,
        story={"product_role": "product", "architect_role": "architect"},
    ).run(card)
    assert st.blocked is None and st.stage == "review"  # the whole story ran, so the note was posted
    item = board.store.get(card.key)
    layers_notes = [c for c in item.comments if c.text.startswith(NOTE_PREFIX + "layers, bottom first:")]
    assert len(layers_notes) == 1  # exactly the layers note
    [layers_note] = layers_notes
    assert layers_note.author == "architect"  # the role that wrote it, not airlie
    assert layers_note.author != "airlie"
    assert layers_note.text.startswith(NOTE_PREFIX)  # still recognisable as airlie's


async def test_a_layer_built_note_is_stored_by_the_build_role_not_airlie(tmp_path: Path) -> None:
    """AC1: each "layer built" note is the build role's work, so it is stored with author "coder"
    (not "airlie") while its text still begins with the ``**airlie** · `` prefix."""
    board, card = _local_board(tmp_path, {"product", "architect", "coder"})
    repo = repo_with_marker_tests(tmp_path)
    st = await pipeline(
        tmp_path,
        repo,
        happy_engine(),
        board=board,
        story={"product_role": "product", "architect_role": "architect", "build_role": "coder"},
    ).run(card)
    assert st.blocked is None and st.stage == "review"  # the whole story ran, so the notes were posted
    item = board.store.get(card.key)
    built_notes = [c for c in item.comments if "built:" in c.text and c.text.startswith(NOTE_PREFIX)]
    assert len(built_notes) == 2  # one per built layer (Storage, Profile)
    for built_note in built_notes:
        assert built_note.author == "coder"  # the build role, not airlie
        assert built_note.author != "airlie"
        assert built_note.text.startswith(NOTE_PREFIX)  # still recognisable as airlie's
