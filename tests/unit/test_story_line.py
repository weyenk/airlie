"""The line's scheduler (P5b of the planning line, 2026-10-25): two lanes under two locks, the planning lane's
order, the build phase file and the gap wait, the pause switch, and the beat that ties them."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from airlie.engine import FakeEngine
from airlie.story import factory
from airlie.story.factory import tick
from airlie.story.line import Line
from airlie.story.local_board import LocalBoard
from airlie.story.state import lane_lock, lock_held
from tests.unit.test_story_epic_pipeline import plan_obj, reply
from tests.unit.test_story_factory import started_log
from tests.unit.test_story_tick_epics import TickRig, specs_rig

# ---- Task 6: two lanes, two locks ----


def test_two_lanes_take_two_locks_and_a_job_holds_exactly_one(tmp_path: Path) -> None:
    stories = tmp_path / "stories"
    with lane_lock(stories, "build") as build:
        assert build
        assert lock_held(stories, "build") and not lock_held(stories, "plan")
        with lane_lock(stories, "build") as again:
            assert not again  # a lane is one job at a time, in this process too
        with lane_lock(stories, "plan") as plan:
            assert plan  # the other lane is free while the build runs
            assert lock_held(stories, "plan")
    assert not lock_held(stories, "build") and not lock_held(stories, "plan")
    assert (stories / "build.lock").is_file() and (stories / "plan.lock").is_file()


def test_the_old_lock_file_is_renamed_once_so_a_running_ticks_lock_is_not_lost(tmp_path: Path) -> None:
    stories = tmp_path / "stories"
    stories.mkdir()
    old = stories / ".lock"
    with old.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # an older airlie's tick, still running
        with lane_lock(stories, "build") as held:
            assert not held  # its lock is the build lane's, under its new name
        assert not old.exists() and (stories / "build.lock").exists()
    assert not lock_held(stories, "build")


# ---- the lanes' picks ----


def story_record(rig: TickRig, key: str, stage: str) -> None:
    folder = rig.cfg.observability.state_dir / "stories" / key
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(json.dumps({"stage": stage, "card": {"key": key}}))


def test_the_build_lane_takes_only_a_planned_story_and_the_planning_lane_plans_the_unplanned_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = TickRig(tmp_path, monkeypatch)
    keys = [rig.store.create(level="story", title=t, labels=["repo:o/r"]).key for t in "abcd"]
    for k in keys[:3]:
        rig.store.update(k, status="approved")
    story_record(rig, keys[1], "layers")  # b: planned (the boundary stage)
    story_record(rig, keys[2], "refine")  # c: started, not planned
    board = factory.make_board(rig.cfg)
    assert isinstance(board, LocalBoard)

    def key_of(item: object) -> str | None:
        return getattr(item, "key", None)

    assert key_of(board.next_unplanned_story()) == keys[0]
    assert key_of(board.next_planned_story()) == keys[1]
    rig.store.update(keys[0], status="doing")
    assert key_of(board.next_unplanned_story()) == keys[2]  # a doing one is being planned
    rig.store.update(
        keys[1], status="doing"
    )  # planned and its card moved to building: still the build lane's
    assert key_of(board.next_planned_story()) == keys[1]
    rig.store.update(keys[1], status="todo")  # a person parked it
    assert board.next_planned_story() is None


async def test_the_planning_lanes_order_is_initiative_epic_revision_epic_plan_then_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = specs_rig(tmp_path, monkeypatch, FakeEngine({}))
    initiative = rig.store.create(level="initiative", title="I", description="x", labels=["repo:o/r"]).key
    revising = rig.store.create(
        level="epic", title="R", description="x", labels=["repo:o/r", f"initiative:{initiative}"]
    ).key
    epic = rig.epic()
    story = rig.store.create(level="story", title="S", labels=["repo:o/r"]).key
    rig.store.update(initiative, status="approved")
    rig.store.update(revising, status="review")
    rig.store.comment(revising, "noah", "make it smaller")
    rig.store.update(story, status="approved")
    board = factory.make_board(rig.cfg)
    assert isinstance(board, LocalBoard)
    line = Line(rig.cfg, board=board, state_dir=rig.cfg.observability.state_dir)
    order = []
    for _ in range(4):
        job = line._pick_plan()
        assert job is not None
        order.append((job.what, job.key))
        if job.what == "initiative":
            rig.store.update(initiative, status="done")
        elif job.what == "revision":
            rig.store.comment(revising, "airlie", "Revised: smaller")  # answered: the person's is not on top
        elif job.what == "epic":
            rig.store.update(epic, status="blocked")
        else:
            break
    # the specs phase's own order (P4): a person's comment is answered before a new initiative is started
    assert order == [
        ("revision", revising),
        ("initiative", initiative),
        ("epic", epic),
        ("story", story),
    ]


async def test_one_beat_runs_a_job_in_each_lane_and_hands_a_fresh_story_to_the_build_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = TickRig(tmp_path, monkeypatch, FakeEngine({"architect": [reply(plan_obj())]}))
    ready = rig.store.create(level="story", title="ready", labels=["repo:o/r"]).key
    rig.store.update(ready, status="approved")
    story_record(rig, ready, "layers")  # planned earlier: the build lane's
    epic = rig.epic()
    started = started_log(monkeypatch)
    line = await tick(rig.cfg)
    # the planning lane planned the epic while the build lane built the planned story, in the same beat
    assert f"epic {epic} Avatars: planned into 2 stories" in line, line
    assert f"{ready} ready: review (taken from approved)" in line, line
    assert [c.key for c in started] == [ready]
    rig.store.update(ready, status="review")  # (the fake pipeline does not move its card)
    # a story nobody planned (the epic's first): the planning lane plans it and, the build lane being free,
    # the one-shot beat hands it over and builds it
    [first, _] = rig.stories(epic)
    assert await tick(rig.cfg) == f"{first} Do S1: review (taken from approved)"
    assert [c.key for c in started] == [ready, first]


async def test_a_beat_that_finds_a_lane_held_by_another_process_logs_busy_and_runs_the_other_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serve ticker and the launchd tick at the same moment (Review Focus 5): the lane one holds is
    never run twice; the other lane goes on."""
    rig = TickRig(tmp_path, monkeypatch, FakeEngine({"architect": [reply(plan_obj())]}))
    ready = rig.store.create(level="story", title="ready", labels=["repo:o/r"]).key
    rig.store.update(ready, status="approved")
    story_record(rig, ready, "layers")
    epic = rig.epic()
    started = started_log(monkeypatch)
    stories = factory.stories_dir(rig.cfg)
    with lane_lock(stories, "plan") as held:  # another process's planning job
        assert held
        line = await tick(rig.cfg)
    assert "busy: the planning lane is in use" in line, line
    assert f"{ready} ready: review (taken from approved)" in line, line
    assert [c.key for c in started] == [ready]
    assert (
        rig.status(epic) == "approved" and not rig.engine.requests
    )  # the epic was not planned twice or at all
    rig.store.update(ready, status="review")
    with lane_lock(stories, "build") as held:  # and the other way round
        assert held
        line = await tick(rig.cfg)
    assert (
        "busy: the build lane is in use" in line and f"epic {epic} Avatars: planned into 2 stories" in line
    ), line
    both = []
    with lane_lock(stories, "plan"), lane_lock(stories, "build"):
        both.append(await tick(rig.cfg))
    assert both == ["busy: the planning lane is in use; busy: the build lane is in use"]
    events = [
        json.loads(x)
        for x in (rig.cfg.observability.state_dir / "line" / "events.jsonl").read_text().splitlines()
    ]
    assert [e["kind"] for e in events].count("lane_start") == 2  # one job per lane that was free, no more
