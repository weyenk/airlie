"""What is running now, whoever started it: the web app, the launchd story tick, a `task` command. One row per
story or standalone run, each with a progress bar, read from the Studio's state directory alone."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from airlie.serve.app import create_app
from airlie.serve.running import ACTIVE_S, run_progress, running, story_progress
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream, cfg

NOW = 1_000_000.0


def ev(kind: str, ts: float, **fields: Any) -> dict[str, Any]:
    return {"ts": ts, "run_id": "r", "kind": kind, **fields}


STUDIO_START = ev("run_start", 1.0, goal="fix the build", k=1, repo="/r", orchestrator="studio")
STUDIO_ONE_OF_FOUR_DONE = [
    STUDIO_START,
    ev("plan", 2.0, subtasks=["fix", "note"], roles=["coder", "docs"]),
    ev("subtask_start", 3.0, subtask="fix", role="coder"),
    ev("subtask_done", 4.0, subtask="fix", ok=True, steps=3, stop_reason="done"),
]
"""A studio run part way: the chief, two subtasks and the reviewer, one subtask finished."""


def touch(path: Path, mtime: float) -> Path:
    os.utime(path, (mtime, mtime))
    return path


def write_story(state: Path, key: str, mtime: float, *, raw: str | None = None, **fields: Any) -> Path:
    """A story directory with only a state.json (the card, then `fields`; or `raw` as written) written at
    `mtime`: the story's last activity."""
    d = state / "stories" / key
    d.mkdir(parents=True)
    f = d / "state.json"
    f.write_text(raw if raw is not None else json.dumps({"card": {"key": key}, **fields}))
    touch(f, mtime)
    return d


def write_run(state: Path, run_id: str, mtime: float, events: list[dict[str, Any]] | None = None) -> Path:
    """A run directory whose event log (the lines of `events`; none: no log) last changed at `mtime`."""
    d = state / "runs" / run_id
    d.mkdir(parents=True)
    if events is not None:
        log = d / "events.jsonl"
        log.write_text("".join(json.dumps(e) + "\n" for e in events))
        touch(log, mtime)
    return d


def listed(state: Path, **kw: Any) -> list[tuple[str, str]]:
    return [(r["kind"], r["key"]) for r in running(state, now=NOW, **kw)]


def test_story_progress() -> None:
    assert story_progress("refine", 0, 0) == 0.4
    assert story_progress("layers", 4, 1) == 0.5
    assert story_progress("publish", 2, 2) == 1.0


def test_story_progress_by_stage_with_unknown_stages_after_refine_and_a_bar_that_never_overflows() -> None:
    assert story_progress("new", 0, 0) == 0.0  # nothing finished yet
    assert story_progress("intake", 0, 0) == 0.2
    assert story_progress("layers", 3, 1) == 0.571  # 4 of 3 + 3 + 1, rounded to three places
    assert story_progress("build", 0, 0) == 0.6  # no layers known counts as one
    assert story_progress("review", 3, 3) == 1.0
    assert story_progress("somewhere-new", 2, 0) == 0.5  # a stage this code does not know is past refine
    assert story_progress("build", 2, 9) == 1.0  # more layers built than planned (a re-plan): full, not over


def node(state: str) -> dict[str, Any]:
    return {"id": state, "state": state}


def test_run_progress_counts_finished_nodes_and_is_none_for_a_single_agent() -> None:
    states = ["done", "failed", "active", "waiting"]
    assert run_progress({"nodes": [node(s) for s in states]}) == 0.5  # done and failed both count
    assert run_progress({"nodes": [node("done"), node("active"), node("idle")]}) == 0.333
    assert run_progress({"nodes": [node("done"), node("stuck")]}) == 0.5
    assert run_progress({"nodes": [node("done"), node("done")]}) == 1.0
    assert run_progress({"nodes": [node("active")]}) is None  # one agent: nothing to count, an unmeasured bar
    assert run_progress({"nodes": [node("done")]}) is None
    assert run_progress({"nodes": []}) is None


def test_running_shows_a_tick_story_and_a_standalone_run_but_not_the_story_own_runs(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_story(
        state,
        "AIR-7",
        NOW - 100,
        card={"key": "AIR-7", "title": "Add avatars"},
        stage="layers",
        layers=[{"title": "one"}, {"title": "two"}],
        built={"01": {"branch": "airlie/x"}},
        runs=["r1"],
    )
    write_run(state, "r1", NOW - 50, [STUDIO_START])  # the story's own run, started, not done
    write_run(state, "r2", NOW - 200, STUDIO_ONE_OF_FOUR_DONE)
    out = running(state, now=NOW)
    assert out == [
        {
            "kind": "story",
            "key": "AIR-7",
            "title": "Add avatars",
            "topology": None,  # a story is a pipeline of runs: it has no topology of its own
            "progress": 0.667,  # 3 stages + 1 layer built, of 3 + 2 layers + 1
            "last_activity": NOW - 50,  # the story's last run wrote last
        },
        {
            "kind": "run",
            "key": "r2",
            "title": "fix the build",
            "topology": "studio",
            "progress": 0.25,  # one of the chief, two subtasks and the reviewer is done
            "last_activity": NOW - 200,
        },
    ]
    assert listed(state) == [("story", "AIR-7"), ("run", "r2")]


def test_running_lists_the_newest_activity_first_whatever_the_kind_and_ties_by_key(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_story(state, "AIR-2", NOW - 300, stage="build")
    write_story(state, "AIR-1", NOW - 300, stage="build")
    write_run(state, "r-new", NOW - 10, [STUDIO_START])
    write_run(state, "r-old", NOW - 500, [STUDIO_START])
    assert listed(state) == [("run", "r-new"), ("story", "AIR-1"), ("story", "AIR-2"), ("run", "r-old")]


def test_a_finished_blocked_or_review_story_and_a_finished_or_unstarted_run_are_not_running(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    write_story(state, "AIR-1", NOW - 10, stage="publish")  # running: its last stage is the stack
    write_story(state, "AIR-2", NOW - 10, stage="review")  # done: the PRs are up
    write_story(state, "AIR-3", NOW - 10, stage="build", blocked="layer 01 failed its gate")
    write_story(state, "AIR-4", NOW - 10, raw="{not json")  # damaged: it says so as blocked
    (state / "stories" / "AIR-5").mkdir()  # an empty directory: nothing to date it by
    write_run(state, "r-done", NOW - 10, [STUDIO_START, ev("run_done", 9.0, ok=True, wall_ms=8000.0)])
    write_run(state, "r-failed", NOW - 10, [STUDIO_START, ev("run_done", 9.0, ok=False, wall_ms=8000.0)])
    write_run(state, "r-no-start", NOW - 10, [ev("step", 2.0, team_id="x", step=1, tool="read_file")])
    write_run(state, "r-no-log", NOW - 10)
    (state / "runs" / "r-not-a-dir").write_text("x")
    write_run(state, "r-active", NOW - 20, [STUDIO_START])
    assert listed(state) == [("story", "AIR-1"), ("run", "r-active")]


def test_a_story_with_no_state_yet_that_is_recent_is_running_at_the_start(tmp_path: Path) -> None:
    state = tmp_path / "state"
    d = state / "stories" / "AIR-9"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("")
    touch(d / "events.jsonl", NOW - 5)
    [row] = running(state, now=NOW)
    assert (row["kind"], row["key"], row["title"], row["progress"]) == ("story", "AIR-9", None, 0.0)


def test_a_run_of_any_story_leaves_the_list_even_when_that_story_is_not_running(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_story(state, "AIR-2", NOW - 10, stage="review", runs=["r-old", "r-last"])
    write_run(state, "r-old", NOW - 10, [STUDIO_START])  # it crashed without a run_done; the story went on
    write_run(state, "r-last", NOW - 10, [STUDIO_START])
    write_run(state, "r-mine", NOW - 10, [STUDIO_START])
    assert listed(state) == [("run", "r-mine")]


def test_a_run_silent_for_an_hour_without_run_done_leaves_running(tmp_path: Path) -> None:
    assert ACTIVE_S == 3600.0
    state = tmp_path / "state"
    d = write_run(state, "r2", NOW - 3601, [STUDIO_START])
    assert listed(state) == []  # it crashed: no run_done will ever come
    touch(d / "events.jsonl", NOW - 3599)
    assert listed(state) == [("run", "r2")]
    touch(d / "events.jsonl", NOW - 3600)
    assert listed(state) == [("run", "r2")]  # an hour exactly is still within it


def test_a_story_silent_for_an_hour_leaves_running(tmp_path: Path) -> None:
    state = tmp_path / "state"
    d = write_story(state, "AIR-7", NOW - 3601, stage="build")
    assert listed(state) == []
    touch(d / "state.json", NOW - 3599)
    assert listed(state) == [("story", "AIR-7")]
    touch(d / "state.json", NOW - 3601)
    (d / "events.jsonl").write_text("")
    touch(d / "events.jsonl", NOW - 3599)  # the story's own log counts
    assert listed(state) == [("story", "AIR-7")]


def test_a_live_job_stays_running_however_quiet(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_run(state, "r2", NOW - 7200, [STUDIO_START])
    write_story(state, "AIR-7", NOW - 7300, stage="build")
    assert listed(state) == []
    assert listed(state, live=frozenset({"r2"})) == [("run", "r2")]
    assert listed(state, live=frozenset({"story-AIR-7"})) == [("story", "AIR-7")]
    assert listed(state, live=frozenset({"r2", "story-AIR-7"})) == [("run", "r2"), ("story", "AIR-7")]
    assert listed(state, live=frozenset({"AIR-7", "story-r2", "r3"})) == []  # ids, not look-alikes


def test_a_live_job_does_not_bring_back_a_finished_run_a_blocked_or_finished_story_or_a_story_run(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    write_run(state, "r-done", NOW - 7200, [STUDIO_START, ev("run_done", 9.0, ok=True, wall_ms=1.0)])
    write_run(state, "r-story", NOW - 7200, [STUDIO_START])
    write_story(state, "AIR-1", NOW - 7200, stage="build", blocked="stopped", runs=["r-story"])
    write_story(state, "AIR-2", NOW - 7200, stage="review")
    live = frozenset({"r-done", "r-story", "story-AIR-1", "story-AIR-2"})
    assert listed(state, live=live) == []


def test_only_the_newest_runs_are_looked_at(tmp_path: Path) -> None:
    state = tmp_path / "state"
    for name in ("20261024-100000-aaaa", "20261024-110000-bbbb", "20261024-120000-cccc"):
        write_run(state, name, NOW - 10, [STUDIO_START])
    assert listed(state) == [
        ("run", "20261024-100000-aaaa"),
        ("run", "20261024-110000-bbbb"),
        ("run", "20261024-120000-cccc"),
    ]
    assert listed(state, recent_runs=2) == [("run", "20261024-110000-bbbb"), ("run", "20261024-120000-cccc")]
    assert listed(state, recent_runs=1) == [("run", "20261024-120000-cccc")]


def test_nothing_is_running_in_a_state_directory_with_no_stories_or_runs(tmp_path: Path) -> None:
    assert running(tmp_path / "nowhere", now=NOW) == []
    (tmp_path / "state").mkdir()
    assert running(tmp_path / "state", now=NOW) == []


def test_a_damaged_state_or_log_skips_its_entry_and_raises_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_story(state, "AIR-1", NOW - 10, stage="build", runs=["a\0b", "../x", 7])  # ids that name no run
    write_story(state, "AIR-2", NOW - 20, raw="[1, 2]")  # JSON, but not a story
    write_story(state, "AIR-3", NOW - 30, stage=7)  # a stage that is no text
    write_story(state, "AIR-4", NOW - 40, stage=["build"])
    write_story(state, "AIR-5", NOW - 50, stage="build", layers="many", built=["x"], runs="r1")
    write_run(state, "r-binary", NOW - 10, None)
    (state / "runs" / "r-binary" / "events.jsonl").write_bytes(b"\xff\xfe\x00 not text\n")
    touch(state / "runs" / "r-binary" / "events.jsonl", NOW - 10)
    write_run(state, "r-odd", NOW - 10, [STUDIO_START, ev("plan", 2.0, subtasks=5)])  # no tree can be drawn
    write_run(state, "r-ok", NOW - 5, [STUDIO_START])
    out = running(state, now=NOW)
    assert [(r["kind"], r["key"]) for r in out] == [
        ("run", "r-ok"),
        ("story", "AIR-1"),
        ("story", "AIR-5"),
    ]
    assert [r["progress"] for r in out if r["kind"] == "story"] == [0.6, 0.6]  # no layers or builds known


def test_the_running_route_lists_what_is_running_with_this_servers_jobs_as_live(tmp_path: Path) -> None:
    config = cfg(tmp_path)
    state = tmp_path / "state"
    write_story(state, "AIR-7", NOW - 7200, stage="layers", card={"key": "AIR-7", "title": "Add avatars"})
    write_story(state, "AIR-8", NOW - 7200, stage="build")  # as quiet, but no job of this server
    write_run(state, "r-tick", NOW - 20, STUDIO_ONE_OF_FOUR_DONE)  # another process's run, active now

    async def hang_story(cfg, **kw):  # type: ignore[no-untyped-def]
        async def work() -> dict[str, Any]:
            await asyncio.sleep(3600)
            return {}

        return "story-AIR-7", tmp_path / "events.jsonl", work()

    app = create_app(
        config,
        transport=httpx.MockTransport(FakeUpstream()),
        story_job=hang_story,
        api_key="k",
        now=lambda: NOW,
    )
    auth = {"Authorization": "Bearer k"}
    with TestClient(app) as c:
        assert c.get("/v1/airlie/running").status_code == 401
        before = c.get("/v1/airlie/running", headers=auth).json()
        assert [(r["kind"], r["key"]) for r in before["running"]] == [("run", "r-tick")]
        assert c.post("/v1/airlie/stories", json={"card": "AIR-7"}, headers=auth).status_code == 202
        after = c.get("/v1/airlie/running", headers=auth).json()
        assert list(after) == ["running"]
        assert [(r["kind"], r["key"]) for r in after["running"]] == [("run", "r-tick"), ("story", "AIR-7")]
        story = after["running"][1]
        assert (
            story["title"] == "Add avatars"
            and story["progress"] == 0.6
            and story["last_activity"] == NOW - 7200
        )
        assert c.delete("/v1/airlie/jobs/story-AIR-7", headers=auth).json()["cancelled"] is True
        c.get("/v1/airlie/jobs/story-AIR-7", params={"since": 10_000, "wait": 5}, headers=auth)
        gone = c.get("/v1/airlie/running", headers=auth).json()
        assert [(r["kind"], r["key"]) for r in gone["running"]] == [("run", "r-tick")]
