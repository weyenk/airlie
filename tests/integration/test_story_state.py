"""StoryState pinned against a real checkout: the github.com repository a story records when it starts, and
the one a story without a pin takes the next time it is opened. The rest of StoryState's behaviour is pinned
in tests/unit/test_story_state.py."""

import json
from pathlib import Path

import pytest

from airlie.story.board import text_card
from airlie.story.model import StorySetupError
from airlie.story.state import StoryState


def test_a_story_records_the_github_repository_it_was_started_for_and_holds_it(tmp_path: Path) -> None:
    """The repository the remote named when the story started: a later run that finds another one there (a
    remote a model rewrote, or another checkout) is a story that cannot be opened, which tick sets aside."""
    card = text_card("x")
    story_dir = tmp_path / card.key
    st = StoryState.open(story_dir, card, tmp_path / "repo", github_repo="weyenk/OpenHand")
    assert st.github_repo == "weyenk/OpenHand"
    assert json.loads((story_dir / "state.json").read_text())["github_repo"] == "weyenk/OpenHand"
    assert StoryState.open(story_dir, card, tmp_path / "repo", github_repo="Weyenk/openhand").github_repo == (
        "weyenk/OpenHand"  # GitHub's names are any case
    )
    f = story_dir / "state.json"
    for now, named in (("weyenk/other", "weyenk/other"), (None, "no github.com repository")):
        with pytest.raises(StorySetupError) as e:
            StoryState.open(story_dir, card, tmp_path / "repo", github_repo=now)
        assert str(e.value) == (
            f"{f}: this story publishes to weyenk/OpenHand, but the remote now names {named}; point the "
            f"remote back at weyenk/OpenHand, or delete {f} to start the story over"
        )


def test_a_story_without_a_github_repository_takes_the_one_it_is_next_opened_with(tmp_path: Path) -> None:
    card = text_card("x")
    story_dir = tmp_path / card.key
    assert StoryState.open(story_dir, card, tmp_path / "repo").github_repo is None  # a local remote, say
    assert StoryState.open(story_dir, card, tmp_path / "repo").github_repo is None
    st = StoryState.open(story_dir, card, tmp_path / "repo", github_repo="o/r")  # a state from before the pin
    assert (
        st.github_repo == "o/r" and json.loads((story_dir / "state.json").read_text())["github_repo"] == "o/r"
    )
