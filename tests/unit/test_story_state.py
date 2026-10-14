"""Story state survives a restart, refuses a different repository, and the lock admits one story at a time."""

import json
from pathlib import Path

import pytest

from airlie.story.board import text_card
from airlie.story.model import StorySetupError
from airlie.story.state import StoryState, story_lock


def test_state_round_trips_and_tracks_stages(tmp_path: Path) -> None:
    card = text_card("Let users upload an avatar")
    st = StoryState.open(tmp_path / card.key, card, tmp_path / "repo")
    assert st.stage == "new" and not st.done("intake")
    st.built["01"] = {"branch": "b1", "sha": "abc"}
    st.finish("intake")
    again = StoryState.open(tmp_path / card.key, card, tmp_path / "repo")
    assert again.stage == "intake" and again.done("intake") and not again.done("refine")
    assert again.built == {"01": {"branch": "b1", "sha": "abc"}} and again.card["title"] == card.title
    kinds = [json.loads(x)["kind"] for x in (tmp_path / card.key / "events.jsonl").read_text().splitlines()]
    assert kinds == ["stage"]


def test_a_story_resumes_only_in_its_own_repository(tmp_path: Path) -> None:
    card = text_card("x")
    StoryState.open(tmp_path / card.key, card, tmp_path / "a")
    with pytest.raises(StorySetupError, match="started in"):
        StoryState.open(tmp_path / card.key, card, tmp_path / "b")


def test_the_lock_admits_one_story_at_a_time(tmp_path: Path) -> None:
    with story_lock(tmp_path) as first:
        assert first
        with story_lock(tmp_path) as second:
            assert not second
    with story_lock(tmp_path) as again:
        assert again
