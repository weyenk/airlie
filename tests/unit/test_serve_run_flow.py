"""A run of any topology as the workflow view draws it, in the shape of a story's flow: the stages its
topology runs left to right, a lane of jobs in each, one per agent; the job detail under it (what a job was
opened with and the steps it took, from the run's trace); and the routes. Built from recorded event logs, no
engine, except the route tests that follow a real plan job to its end."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.engine import FakeEngine, FakeReply
from airlie.obs.run_dir import RunDir
from airlie.serve.app import create_app
from airlie.serve.flow import OBSERVATION_CHARS, job_detail, trace_of
from airlie.serve.run_flow import run_flow, run_job_detail
from airlie.serve.tree import RunNotFound
from tests.unit.serve_helpers import TestClient
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_serve import FakeUpstream
from tests.unit.test_serve_jobs import app_for, finish
from tests.unit.test_serve_tree import FORK, STUDIO_DONE, STUDIO_START, ev, step
from tests.unit.test_serve_tree import write_run as write_log
from tests.unit.test_story_flow import KEY as STORY
from tests.unit.test_story_flow import write_story
from tests.unit.test_studio_orchestrator import PLAN, cfg_for


def write_run(state: Path, run: str, events: list[dict[str, Any]]) -> Path:
    """Run `run`'s event log under `state`; returns `state`."""
    write_log(state, run, events)
    return state


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def node(flow: dict[str, Any], id: str) -> dict[str, Any]:
    (found,) = [n for n in flow["nodes"] if n["id"] == id]
    return found


def lanes(flow: dict[str, Any]) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Each stage's id and status, with its jobs' ids and statuses."""
    return [(n["id"], n["status"], [(j["id"], j["status"]) for j in n["jobs"]]) for n in flow["nodes"]]


def running_anything(flow: dict[str, Any]) -> bool:
    """Whether any stage or job of the flow says it is running."""
    return any(
        n["status"] == "running" or any(j["status"] == "running" for j in n["jobs"]) for n in flow["nodes"]
    )


# ---- the studio ----


def test_a_studio_run_part_way_has_plan_specialists_and_who_is_working(tmp_path: Path) -> None:
    f = run_flow(write_run(tmp_path, "r1", STUDIO_START), "r1")
    assert [n["id"] for n in f["nodes"]] == ["plan", "specialists", "review"]
    spec = node(f, "specialists")
    assert [(j["id"], j["status"]) for j in spec["jobs"]] == [("fix", "running"), ("note", "pending")]
    assert f["topology"] == "studio" and f["stage"] == "running"
    shape = {"key", "kind", "topology", "mode", "title", "summary", "stage", "blocked", "acceptance", "ok"}
    assert set(f) == {*shape, "progress", "nodes", "repo"}
    assert all(set(n) == {"id", "label", "kind", "status", "jobs"} for n in f["nodes"])
    assert {k: f[k] for k in ("key", "kind", "mode", "title", "summary", "blocked", "acceptance", "ok")} == {
        "key": "r1",
        "kind": "run",
        "mode": "run",
        "title": "fix the build",
        "summary": None,
        "blocked": None,
        "acceptance": [],
        "ok": None,
    }
    assert f["repo"] == "/r"  # the repository the run is for, which Approve plan sends back
    assert f["progress"] == 0.0  # four nodes in the tree (chief, fix, note, reviewer), none finished
    assert lanes(f) == [
        ("plan", "running", [("chief", "running")]),  # the chief waits on its specialists: idle
        ("specialists", "running", [("fix", "running"), ("note", "pending")]),
        ("review", "pending", [("reviewer", "pending")]),
    ]
    assert [(n["label"], n["kind"]) for n in f["nodes"]] == [
        ("Plan", "stage"),
        ("Specialists", "stage"),
        ("Review", "stage"),
    ]
    fix, note = spec["jobs"]
    assert fix == {
        "id": "fix",
        "label": "Coder",
        "role": "coder",
        "status": "running",
        "attempts": 1,
        "fix_rounds": 0,
        "retries": [],
        "studio_run": "r1",  # the directory the run was read from, whatever its events say
        "started": None,
        "ended": None,
        "duration_s": None,
        "tokens": {"prompt": 2000, "completion": 200},
        "won": False,
    }
    assert (
        note["attempts"] == 0 and note["label"] == "Docs" and note["tokens"] == {"prompt": 0, "completion": 0}
    )


def test_a_finished_studio_run_adds_synthesis_and_every_stage_passed(tmp_path: Path) -> None:
    f = run_flow(write_run(tmp_path, "r1", STUDIO_DONE), "r1")
    assert lanes(f) == [
        ("plan", "passed", [("chief", "passed")]),
        ("specialists", "passed", [("fix", "passed"), ("note", "passed")]),
        ("review", "passed", [("reviewer", "passed")]),
        ("synthesis", "passed", [("synthesis", "passed")]),
    ]
    assert f["stage"] == "done" and f["ok"] is True and f["progress"] == 1.0
    assert node(f, "specialists")["jobs"][0]["fix_rounds"] == 1


def test_a_failed_subtask_fails_its_stage_and_a_run_that_ended_with_no_review_has_none(
    tmp_path: Path,
) -> None:
    events = [
        *STUDIO_START,
        ev("subtask_done", 5.0, subtask="fix", role="coder", ok=False, stop_reason="max_steps", steps=2),
        ev("run_done", 6.0, ok=False, wall_ms=5000.0, subtasks=1, tests=False),
    ]
    f = run_flow(write_run(tmp_path, "r1", events), "r1")
    assert lanes(f) == [
        ("plan", "passed", [("chief", "passed")]),
        ("specialists", "failed", [("fix", "failed"), ("note", "pending")]),
    ]
    assert f["stage"] == "done" and f["ok"] is False


def test_a_failed_job_beats_a_running_one_and_some_passed_with_some_waiting_is_pending(
    tmp_path: Path,
) -> None:
    events = [
        *STUDIO_START,
        ev("subtask_done", 5.0, subtask="note", role="docs", ok=False, stop_reason="max_steps", steps=1),
    ]
    running_and_failed = run_flow(write_run(tmp_path, "r1", events), "r1")
    assert lanes(running_and_failed)[1] == ("specialists", "failed", [("fix", "running"), ("note", "failed")])
    done = [
        *STUDIO_START,
        ev("subtask_done", 5.0, subtask="fix", role="coder", ok=True, stop_reason="done", steps=2),
    ]
    passed_and_waiting = run_flow(write_run(tmp_path, "r2", done), "r2")
    assert lanes(passed_and_waiting)[1] == (
        "specialists",
        "pending",
        [("fix", "passed"), ("note", "pending")],
    )


def test_a_studio_run_still_planning_has_no_specialists_yet(tmp_path: Path) -> None:
    f = run_flow(write_run(tmp_path, "r1", STUDIO_START[:1]), "r1")
    assert lanes(f) == [("plan", "running", [("chief", "running")]), ("specialists", "pending", [])]
    assert f["progress"] is None  # one node: no bar to count


def test_a_plan_mode_run_has_only_its_plan(tmp_path: Path) -> None:
    events = [
        ev("run_start", 1.0, goal="g", k=1, repo="/r", orchestrator="studio", mode="plan"),
        ev("plan", 2.0, subtasks=["a"], roles=["coder"]),
        ev("run_done", 3.0, ok=True, wall_ms=2000.0, subtasks=0, tests=None),
    ]
    f = run_flow(write_run(tmp_path, "r1", events), "r1")
    assert f["mode"] == "plan" and lanes(f) == [("plan", "passed", [("chief", "passed")])]


# ---- teams, single, fork ----


def test_a_teams_run_has_a_lane_per_team_and_the_judge(tmp_path: Path) -> None:
    start = [
        ev("run_start", 1.0, goal="pick one", k=2, repo="/r", orchestrator="native"),
        ev("team_start", 1.1, team_id="t1", worktree="/w/1"),
        ev("team_start", 1.1, team_id="t2", worktree="/w/2"),
        step("t1", 1, 2.0),
    ]
    working = run_flow(write_run(tmp_path, "r1", start), "r1")
    assert working["topology"] == "teams" and working["title"] == "pick one"
    assert lanes(working) == [("teams", "running", [("t1", "running"), ("t2", "running")])]
    judged = [
        *start,
        ev("team_done", 3.0, team_id="t1", ok=True, stop_reason="done", steps=1),
        ev("team_done", 3.0, team_id="t2", ok=True, stop_reason="done", steps=3),
        ev("aggregate", 4.0, winner="t2", reason="tests pass", scores={"t1": 0.5, "t2": 1.0}),
        ev("run_done", 5.0, ok=True, wall_ms=4000.0),
    ]
    f = run_flow(write_run(tmp_path, "r2", judged), "r2")
    assert [n["id"] for n in f["nodes"]] == ["teams", "judge"]
    assert lanes(f) == [
        ("teams", "passed", [("t1", "passed"), ("t2", "passed")]),
        ("judge", "passed", [("aggregate", "passed")]),
    ]
    t1, t2 = node(f, "teams")["jobs"]
    assert (t1["label"], t1["won"]) == ("Team 1", False) and (t2["label"], t2["won"]) == ("Team 2", True)
    assert t1["tokens"] == {"prompt": 1000, "completion": 100} and t1["role"] == "coder"
    assert [n["label"] for n in f["nodes"]] == ["Teams", "Judge"] and f["progress"] == 1.0


def test_a_teams_run_whose_judge_picked_the_team_that_passed_has_passed_teams(tmp_path: Path) -> None:
    """Teams are attempts side by side: one losing is normal, so the stage passes when any team passed."""
    start = [
        ev("run_start", 1.0, goal="g", k=2, repo="/r", orchestrator="native"),
        ev("team_start", 1.1, team_id="t1", worktree="/w/1"),
        ev("team_start", 1.1, team_id="t2", worktree="/w/2"),
        ev("team_done", 3.0, team_id="t1", ok=False, stop_reason="max_steps", steps=3),
    ]
    one_failed_one_working = run_flow(write_run(tmp_path, "r1", start), "r1")
    assert lanes(one_failed_one_working)[0] == ("teams", "running", [("t1", "failed"), ("t2", "running")])
    judged = [
        *start,
        ev("team_done", 3.0, team_id="t2", ok=True, stop_reason="done", steps=3),
        ev("aggregate", 4.0, winner="t2", reason="tests pass", scores={"t1": 0.0, "t2": 1.0}),
        ev("run_done", 5.0, ok=True, wall_ms=4000.0),
    ]
    f = run_flow(write_run(tmp_path, "r2", judged), "r2")
    assert lanes(f) == [
        ("teams", "passed", [("t1", "failed"), ("t2", "passed")]),
        ("judge", "passed", [("aggregate", "passed")]),
    ]
    t1, t2 = node(f, "teams")["jobs"]
    assert t1["won"] is False and t2["won"] is True
    all_failed = [
        *start,
        ev("team_done", 3.0, team_id="t2", ok=False, stop_reason="max_steps", steps=3),
        ev("run_done", 5.0, ok=False, wall_ms=4000.0),
    ]
    assert lanes(run_flow(write_run(tmp_path, "r3", all_failed), "r3"))[0][:2] == ("teams", "failed")


def test_a_single_run_has_one_agent_and_no_progress_number(tmp_path: Path) -> None:
    events = [
        ev("run_start", 1.0, goal="g", k=1, repo="/r", orchestrator="native"),
        ev("team_start", 1.1, team_id="t1", worktree="/w/1"),
    ]
    f = run_flow(write_run(tmp_path, "r1", events), "r1")
    assert f["topology"] == "single" and f["progress"] is None
    assert lanes(f) == [("agent", "running", [("t1", "running")])]
    assert node(f, "agent")["label"] == "Agent" and node(f, "agent")["jobs"][0]["label"] == "Agent"
    before = run_flow(write_run(tmp_path, "r2", events[:1]), "r2")
    assert lanes(before) == [("agent", "pending", [])]  # started, its agent not yet


def test_a_fork_run_has_the_agent_then_its_forks_and_marks_the_winner(tmp_path: Path) -> None:
    events = [
        ev("run_start", 1.0, goal="g", topology="fork", repo="/r", width=1),
        ev("branch_start", 1.1, branch="b0", worktree="/w/b0", child=False),
        ev("fork_point", 3.1, round=1, parent="b0", reason="stuck", children=["b1"], hypotheses=["h"]),
        ev("branch_start", 3.2, branch="b1", worktree="/w/b1", child=True),
        ev("branch_done", 5.0, branch="b1", ok=True, stop_reason="done", steps=1),
        ev("fork_resolved", 5.1, round=1, winner="b1", how="tests"),
    ]
    f = run_flow(write_run(tmp_path, "r1", events), "r1")
    assert f["topology"] == "fork" and [n["id"] for n in f["nodes"]] == ["agent", "forks"]
    assert lanes(f) == [
        ("agent", "running", [("b0", "running")]),  # stuck: its forks work on
        ("forks", "passed", [("b1", "passed")]),
    ]
    (b1,) = node(f, "forks")["jobs"]
    assert b1["won"] is True and b1["label"] == "Fork 1" and node(f, "agent")["jobs"][0]["won"] is False
    assert [n["label"] for n in f["nodes"]] == ["Agent", "Forks"]
    two = run_flow(write_run(tmp_path, "r2", FORK), "r2")  # two forks, one stepping
    assert lanes(two)[1] == ("forks", "running", [("b1", "running"), ("b2", "running")])
    alone = run_flow(write_run(tmp_path, "r3", FORK[:3]), "r3")  # not stuck yet: no forks
    assert lanes(alone) == [("agent", "running", [("b0", "running")])]
    forked = run_flow(write_run(tmp_path, "r4", FORK[:5]), "r4")  # forked, no fork started yet
    assert lanes(forked)[1] == ("forks", "pending", [("b1", "pending"), ("b2", "pending")])
    one_out = [*FORK[:5], ev("branch_done", 4.0, branch="b1", ok=False, stop_reason="max_steps", steps=0)]
    waiting = run_flow(write_run(tmp_path, "r5", one_out), "r5")  # one fork failed, the other not started
    assert lanes(waiting)[1] == ("forks", "pending", [("b1", "failed"), ("b2", "pending")])


def test_a_finished_fork_run_whose_trunk_ended_stuck_shows_nothing_running(tmp_path: Path) -> None:
    events = [
        *FORK,
        ev("branch_done", 5.0, branch="b1", ok=True, stop_reason="done", steps=1),
        ev("branch_done", 5.0, branch="b2", ok=False, stop_reason="cancelled", steps=0),
        ev("fork_resolved", 5.1, round=1, winner="b1", how="tests"),
        ev("run_done", 6.0, ok=True, wall_ms=5000.0, forks=2, rounds=1, winner="b1"),
    ]
    state = write_run(tmp_path, "r1", events)
    f = run_flow(state, "r1")
    assert f["stage"] == "done" and f["ok"] is True
    assert lanes(f) == [
        ("agent", "failed", [("b0", "failed")]),  # stuck, and the run is over: it never finished
        ("forks", "passed", [("b1", "passed"), ("b2", "failed")]),  # a fork won
    ]
    assert not running_anything(f)
    assert run_job_detail(state, "r1", "agent", "b0")["job"]["status"] == "failed"


def test_a_run_ended_without_its_run_done_shows_nothing_running(tmp_path: Path) -> None:
    """`ended`: the run's job failed or was cancelled, so the log stops short: what was still at work failed,
    what never started stays pending."""
    state = write_run(tmp_path, "r1", STUDIO_START)
    assert run_flow(state, "r1")["stage"] == "running"
    f = run_flow(state, "r1", ended=True)
    assert f["stage"] == "done" and not running_anything(f)
    assert lanes(f) == [
        ("plan", "failed", [("chief", "failed")]),
        ("specialists", "failed", [("fix", "failed"), ("note", "pending")]),
        ("review", "pending", [("reviewer", "pending")]),
    ]
    assert run_job_detail(state, "r1", "specialists", "fix", ended=True)["job"]["status"] == "failed"
    assert run_job_detail(state, "r1", "specialists", "fix")["job"]["status"] == "running"


def test_a_run_with_no_start_yet_has_no_stages_and_an_unknown_run_is_not_found(tmp_path: Path) -> None:
    f = run_flow(write_run(tmp_path, "r1", []), "r1")
    assert f["nodes"] == [] and f["topology"] is None and f["stage"] == "running" and f["title"] is None
    for bad in ("nope", "..", ".", "../stories"):
        with pytest.raises(RunNotFound):
            run_flow(tmp_path, bad)


# ---- the job detail ----


def test_a_run_job_detail_gives_its_prompt_and_every_attempts_steps(tmp_path: Path) -> None:
    state = write_run(tmp_path, "r1", STUDIO_DONE)
    trace = state / "runs" / "r1" / "trace" / "fix"
    write_json(trace / "prompt.json", {"role": "coder", "attempt": 0, "messages": [{"role": "user"}]})
    step_one = {"u_t": {"name": "read_file", "arguments": "{}"}, "o_t": "x", "ok": True}
    write_json(trace / "step-001.json", {**step_one, "assistant_text": "looked", "reasoning": "r"})
    long = {"u_t": {"name": "write_file", "arguments": "{}"}, "o_t": "y" * 5000, "ok": False}
    write_json(trace / "round-1" / "step-001.json", {**long, "assistant_text": "fixed", "reasoning": None})
    d = run_job_detail(state, "r1", "specialists", "fix")
    assert d["node"] == "specialists" and d["job"] == node(run_flow(state, "r1"), "specialists")["jobs"][0]
    assert d["input"] == {"role": "coder", "attempt": 0, "messages": [{"role": "user"}]}
    assert d["input_note"] is None and d["output"] == {"final_text": "fixed"}
    assert [(s["attempt"], s["n"], s["tool"], s["ok"]) for s in d["steps"]] == [
        (0, 1, "read_file", True),
        (1, 1, "write_file", False),
    ]
    assert len(d["steps"][1]["observation"]) == OBSERVATION_CHARS
    chief = run_job_detail(state, "r1", "plan", "chief")  # the chief takes no step: no trace of its own
    assert chief["input"] is None and chief["input_note"] == "opening prompt not recorded"
    assert chief["steps"] == [] and chief["output"] == {"final_text": ""}


def test_a_run_job_detail_404s_what_is_not_in_the_flow(tmp_path: Path) -> None:
    state = write_run(tmp_path, "r1", STUDIO_START)
    for run, stage, job in (("r1", "nope", "fix"), ("r1", "specialists", "nope"), ("r1", "plan", "fix")):
        with pytest.raises(RunNotFound):
            run_job_detail(state, run, stage, job)
    with pytest.raises(RunNotFound):
        run_job_detail(state, "nope", "plan", "chief")


def test_a_job_whose_name_would_leave_the_trace_folder_reads_nothing(tmp_path: Path) -> None:
    events = [
        ev("run_start", 1.0, goal="g", k=1, repo="/r", orchestrator="studio"),
        ev("plan", 2.0, subtasks=[".."], roles=["coder"]),
    ]
    state = write_run(tmp_path, "r1", events)
    (state / "runs" / "r1" / "trace").mkdir()  # as every run directory has (RunDir.create)
    write_json(state / "runs" / "r1" / "prompt.json", {"secret": True})  # where trace/.. points
    d = run_job_detail(state, "r1", "specialists", "..")
    assert d["input"] is None and d["steps"] == []


def test_trace_of_reads_one_subtask_and_refuses_names_that_are_not_one_folder(tmp_path: Path) -> None:
    trace = tmp_path / "runs" / "r1" / "trace" / "fix"
    write_json(trace / "prompt.json", {"role": "coder"})
    write_json(trace / "step-002.json", {"u_t": {"name": "run_tests"}, "o_t": "ok", "ok": True})
    prompt, steps = trace_of(tmp_path, "r1", "fix")
    assert prompt == {"role": "coder"} and [(s["n"], s["tool"]) for s in steps] == [(2, "run_tests")]
    assert trace_of(tmp_path, "r1", "missing") == (None, [])
    for leak in ("runs/r1/trace", "runs/r1", "runs", "trace/x"):  # where each name below would lead
        write_json(tmp_path / leak / "prompt.json", {"leak": leak})
    for run, team in (
        ("r1", "."),
        ("r1", ".."),
        (".", ".."),
        ("..", "x"),
        ("", "fix"),
        ("r1", ""),
        ("r1", "a/b"),
    ):
        assert trace_of(tmp_path, run, team) == (None, []), (run, team)
    (trace / "prompt.json").write_text("[1, 2]")  # not an object: no prompt
    assert trace_of(tmp_path, "r1", "fix")[0] is None
    (trace / "prompt.json").write_text("{torn")
    assert trace_of(tmp_path, "r1", "fix") == (None, steps)


def test_story_job_detail_is_unchanged_after_the_trace_helper(tmp_path: Path) -> None:
    """job_detail reads its trace through trace_of now: pinned to what it returned before that refactor, for
    a job with a recorded prompt, one with a fix round's folder and no prompt, airlie's own check, and stage
    jobs with no trace at all."""
    state = write_story(tmp_path)
    build = state / "runs" / "run-l1" / "trace" / "build"
    first = {"u_t": {"name": "read_file", "arguments": "{}"}, "o_t": "z", "ok": True}
    write_json(build / "step-001.json", {**first, "assistant_text": "first", "reasoning": "r"})
    fix = {"u_t": {"name": "write_file", "arguments": '{"path": "a.py"}'}, "o_t": "y" * 2500, "ok": False}
    write_json(build / "round-1" / "step-001.json", {**fix, "assistant_text": "fixed it", "reasoning": None})
    layer = {
        "acceptance": ["AC1"],
        "brief": "build the seam",
        "scope": "src/a.py",
        "slug": "seam",
        "title": "The seam",
    }
    speed = {"model": None, "prefill_s": 0.5, "prefill_tps": 2000.0, "prompt": 1000, "role": None, "steps": 1}
    assert job_detail(state, STORY, "layer-01", "tests") == {
        "node": "layer-01",
        "job": {
            "id": "tests",
            "label": "Tests",
            "role": "tester",
            "status": "passed",
            "attempts": 1,
            "fix_rounds": 0,
            "retries": [],
            "studio_run": "run-l1",
            "started": 91.0,
            "ended": 150.0,
            "duration_s": 59.0,
            "tokens": {
                **speed,
                "completion": 200,
                "effective_tps": 44.4,
                "gen_s": 4.0,
                "tool_s": 0.0,
                "tps": 50.0,
            },
        },
        "input": {"role": "tester", "attempt": 0, "messages": [{"role": "user", "content": "write tests"}]},
        "input_note": None,
        "output": {"final_text": "", "layer": layer},
        "steps": [
            {
                "attempt": 0,
                "n": 1,
                "tool": "read_file",
                "arguments": "{}",
                "ok": True,
                "observation": "x",
                "assistant_text": "",
                "reasoning": "think",
            }
        ],
    }
    assert job_detail(state, STORY, "layer-01", "build") == {
        "node": "layer-01",
        "job": {
            "id": "build",
            "label": "Build",
            "role": "coder",
            "status": "passed",
            "attempts": 1,
            "fix_rounds": 1,
            "retries": [],
            "studio_run": "run-l1",
            "started": 151.0,
            "ended": 290.0,
            "duration_s": 139.0,
            "tokens": {
                **speed,
                "completion": 100,
                "effective_tps": 40.0,
                "gen_s": 2.0,
                "tool_s": 0.0,
                "tps": 50.0,
            },
        },
        "input": None,
        "input_note": "opening prompt not recorded",
        "output": {
            "final_text": "fixed it",
            "layer": layer,
            "built": {"branch": "b1", "sha": "abc", "run_id": "run-l1"},
        },
        "steps": [
            {
                "attempt": 0,
                "n": 1,
                "tool": "read_file",
                "arguments": "{}",
                "ok": True,
                "observation": "z",
                "assistant_text": "first",
                "reasoning": "r",
            },
            {
                "attempt": 1,
                "n": 1,
                "tool": "write_file",
                "arguments": '{"path": "a.py"}',
                "ok": False,
                "observation": "y" * 2000,
                "assistant_text": "fixed it",
                "reasoning": None,
            },
        ],
    }

    def trace_part(node_id: str, job_id: str) -> dict[str, Any]:
        d = job_detail(state, STORY, node_id, job_id)
        return {k: d[k] for k in ("node", "input", "input_note", "output", "steps")}

    no_trace = {"input": None, "input_note": "opening prompt not recorded", "steps": []}
    assert trace_part("layer-01", "checks") == {
        "node": "layer-01",
        "input": None,
        "input_note": "airlie's own check, no model",
        "output": {"final_text": "", "layer": layer},
        "steps": [],
    }
    assert trace_part("layer-02", "tests") == {
        "node": "layer-02",
        **no_trace,
        "output": {
            "final_text": "",
            "layer": {
                "slug": "wire",
                "title": "Wire it",
                "scope": "src/b.py",
                "brief": "wire the seam",
                "acceptance": [],
            },
        },
    }
    story = {
        "title": "Do the thing",
        "summary": "who wants what",
        "acceptance": [{"id": "AC1", "given": "g", "when": "w", "then": "t"}],
    }
    assert trace_part("refine", "refine") == {
        "node": "refine",
        **no_trace,
        "output": {"final_text": "", "story": story},
    }
    assert trace_part("intake", "intake") == {"node": "intake", **no_trace, "output": {"final_text": ""}}
    layers = trace_part("layers", "layers")
    assert layers["output"]["layers"][0] == layer and len(layers["output"]["layers"]) == 2
    assert {k: layers[k] for k in ("input", "input_note", "steps")} == no_trace


# ---- the routes ----


def test_the_flow_route_adds_the_plan_of_a_finished_plan_job_and_404s_an_unknown_run(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    with TestClient(app_for(tmp_path, FakeEngine({"chief": [PLAN]}))) as client:
        job = client.post("/v1/airlie/plans", json={"goal": "fix the build", "repo": str(repo)}).json()[
            "job_id"
        ]
        planned = finish(client, job)
        assert planned["state"] == "done" and planned["result"]["type"] == "plan"
        r = client.get(f"/v1/airlie/runs/{job}/flow")
        assert r.status_code == 200
        body = r.json()
        assert body["plan"] == planned["result"]["plan"]
        assert [s["id"] for s in body["plan"]["subtasks"]] == ["fix", "note"]
        assert body["job"] == "done" and body["key"] == job and body["kind"] == "run"
        assert body["mode"] == "plan" and lanes(body) == [("plan", "passed", [("chief", "passed")])]
        chief = client.get(f"/v1/airlie/runs/{job}/flow/plan/chief")
        assert chief.status_code == 200 and chief.json()["job"]["id"] == "chief"
        for path in ("nope/flow", "nope/flow/plan/chief", f"{job}/flow/nope/chief", f"{job}/flow/plan/nope"):
            assert client.get(f"/v1/airlie/runs/{path}").status_code == 404, path
        state = tmp_path / "state"
        write_run(state, "20261024-000000-dead", STUDIO_START)  # a run this server did not start
        other = client.get("/v1/airlie/runs/20261024-000000-dead/flow").json()
        assert other["job"] is None and other["plan"] is None and other["stage"] == "running"


def test_a_plan_job_that_answered_instead_has_no_plan_to_approve(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    answer = FakeReply(text=json.dumps({"type": "answer", "answer": "it is 42"}))
    with TestClient(app_for(tmp_path, FakeEngine({"chief": [answer]}))) as client:
        job = client.post("/v1/airlie/plans", json={"goal": "6 x 7?", "repo": str(repo)}).json()["job_id"]
        assert finish(client, job)["result"]["type"] == "answer"
        body = client.get(f"/v1/airlie/runs/{job}/flow").json()
    assert body["job"] == "done" and body["plan"] is None


def test_a_run_whose_job_failed_before_its_run_done_is_done(tmp_path: Path) -> None:
    config = cfg_for(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    class Breaks:
        def __init__(self) -> None:
            self.run_dir = RunDir.create(config.observability.state_dir, "20261024-000000-beef")

        async def plan(self, goal: str, **_: Any) -> Any:
            self.run_dir.events_path.write_text(json.dumps(STUDIO_START[0]) + "\n")  # its run_start, no more
            raise RuntimeError("engine fell over")

    app = create_app(
        config, transport=httpx.MockTransport(FakeUpstream()), orchestrator_factory=lambda *a, **k: Breaks()
    )
    with TestClient(app) as client:
        job = client.post("/v1/airlie/plans", json={"goal": "g", "repo": str(repo)}).json()["job_id"]
        assert finish(client, job)["state"] == "failed"
        body = client.get(f"/v1/airlie/runs/{job}/flow").json()
        chief = client.get(f"/v1/airlie/runs/{job}/flow/plan/chief").json()
    assert body["job"] == "failed" and body["stage"] == "done" and body["plan"] is None
    assert body["topology"] == "studio" and not running_anything(body)
    assert lanes(body) == [("plan", "failed", [("chief", "failed")]), ("specialists", "pending", [])]
    assert chief["job"]["status"] == "failed"  # the job detail agrees with the flow
