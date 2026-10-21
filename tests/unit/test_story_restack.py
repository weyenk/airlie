"""A stopped story's built layers moved onto the trunk as it is now (`task story:restack`): replayed in order
on a checkout of the new trunk and each held to the story's gate there, and only then are the branches moved
and the state's base rewritten, so a resume builds the next layer on today's trunk. A conflict or a red gate
changes nothing. On the marker-test repository, with gh recorded and the trunk fetch mirrored."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from airlie.engine import FakeEngine
from airlie.story.board import text_card
from airlie.story.restack import RestackError
from airlie.story.state import StoryState
from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_pipeline import B1, B2, LAYERS, NOTE, STORY, kinds, pipeline
from tests.unit.test_studio_orchestrator import APPROVE, DONE, RUN, WRITE_PASS

CARD = text_card("avatars")


async def stopped_after_layer_01(tmp_path: Path) -> tuple[Path, StoryState]:
    """A story whose layer 01 is built and whose layer 02 stopped (it changed nothing): its branch and
    worktree are kept, as an attempt."""
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {
            "product": [STORY],
            "architect": [LAYERS],
            "qa": [DONE, DONE],
            "coder": [WRITE_PASS, RUN, DONE, DONE],
            "reviewer": [APPROVE, APPROVE],
        }
    )
    st = await pipeline(tmp_path, repo, engine).run(CARD)
    assert "01" in st.built and st.blocked is not None and "changed nothing" in st.blocked
    assert st.attempt is not None and st.attempt["branch"] == B2
    return repo, st


def trunk_commit(repo: Path, files: dict[str, str], message: str = "trunk moves on") -> str:
    """A commit on the repository's trunk (checked out in `repo`): the mirrored fetch then finds it there."""
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def state_on_disk(st: StoryState) -> dict[str, object]:
    return json.loads((st.path / "state.json").read_text())


async def test_built_layers_move_onto_the_trunk_and_a_resume_builds_on_them(tmp_path: Path) -> None:
    repo, st = await stopped_after_layer_01(tmp_path)
    old_base, old_01 = st.base, st.built["01"]["sha"]
    assert old_base is not None
    news = trunk_commit(repo, {"NEWS": "news\n"})
    line = await pipeline(tmp_path, repo, FakeEngine({})).restack(CARD)
    st = StoryState.open(st.path, CARD, repo)
    assert st.base == {"ref": old_base["ref"], "sha": news}
    new_01 = st.built["01"]["sha"]
    assert new_01 != old_01 and git(repo, "rev-parse", B1) == new_01
    assert git(repo, "rev-parse", f"{B1}^") == news  # one commit, on the new trunk
    assert git(repo, "show", "--name-only", "--format=", B1) == "PASS"  # the same change
    kept = "--format=%an %ae %s"  # its author and message
    assert git(repo, "log", "-1", kept, B1) == git(repo, "log", "-1", kept, old_01)
    # the stopped layer 02 was built on the old layer 01: it goes, as a resume would remove it
    assert st.attempt is None and not git(repo, "branch", "--list", B2)
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1  # no checkout left behind
    [moved] = kinds(st.path / "events.jsonl", "restacked")
    assert moved["base"] == {"from": old_base["sha"], "to": news}
    assert moved["layers"] == {"01": {"from": old_01, "to": new_01}}
    assert "layer 01" in line and news[:12] in line
    assert "restacked onto" in (st.path / "notes.md").read_text()
    # the next run builds layer 02 on the moved layer 01
    again = FakeEngine({"qa": [DONE], "coder": [NOTE, DONE], "reviewer": [APPROVE]})
    st = await pipeline(tmp_path, repo, again).run(CARD)
    assert st.blocked is None and "02" in st.built
    assert git(repo, "rev-parse", f"{B2}^") == new_01
    assert [e["base_sha"] for e in kinds(st.path / "events.jsonl", "layer_start")][-1] == new_01


async def test_a_layer_that_conflicts_with_the_trunk_changes_nothing(tmp_path: Path) -> None:
    repo, st = await stopped_after_layer_01(tmp_path)
    before = state_on_disk(st)
    trunk_commit(repo, {"PASS": "the trunk's own PASS\n"})  # layer 01 adds PASS too
    with pytest.raises(RestackError, match=r"layer 01 .*conflicts with .*PASS"):
        await pipeline(tmp_path, repo, FakeEngine({})).restack(CARD)
    assert state_on_disk(st) == before
    assert git(repo, "rev-parse", B1) == st.built["01"]["sha"]
    assert git(repo, "rev-parse", "--verify", B2)  # the attempt is still there to inspect
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2  # repo + the attempt's
    assert kinds(st.path / "events.jsonl", "restacked") == []


async def test_a_layer_whose_gate_fails_on_the_new_trunk_changes_nothing(tmp_path: Path) -> None:
    repo, st = await stopped_after_layer_01(tmp_path)
    before = state_on_disk(st)
    trunk_commit(repo, {"check.sh": "#!/bin/sh\necho 'trunk broke it'; exit 1\n"})
    with pytest.raises(RestackError, match=r"layer 01 fails `./check.sh` on .*: .*trunk broke it"):
        await pipeline(tmp_path, repo, FakeEngine({})).restack(CARD)
    assert state_on_disk(st) == before
    assert git(repo, "rev-parse", B1) == st.built["01"]["sha"]
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2


async def test_a_story_already_on_the_trunk_is_left_alone(tmp_path: Path) -> None:
    repo, st = await stopped_after_layer_01(tmp_path)
    before = state_on_disk(st)
    line = await pipeline(tmp_path, repo, FakeEngine({})).restack(CARD)
    assert "already on" in line and state_on_disk(st) == before


async def test_a_published_story_is_not_restacked(tmp_path: Path) -> None:
    repo, st = await stopped_after_layer_01(tmp_path)
    st.prs = [{"number": 7, "branch": B1, "url": "u7"}]
    st.save()
    trunk_commit(repo, {"NEWS": "news\n"})
    with pytest.raises(RestackError, match="has published its PRs"):
        await pipeline(tmp_path, repo, FakeEngine({})).restack(CARD)
    assert git(repo, "rev-parse", B1) == st.built["01"]["sha"]


async def test_a_story_that_has_not_started_has_nothing_to_restack(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    with pytest.raises(RestackError, match="has no base yet"):
        await pipeline(tmp_path, repo, FakeEngine({})).restack(CARD)
