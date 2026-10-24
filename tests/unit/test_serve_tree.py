"""The live topology page's tree: a run's agents as nodes laid out by its topology, who is working now, and
the hand-offs between them as edges that say what was handed over. Built from recorded events alone, with no
engine: a running and a finished studio run, a studio run logged before hand-off events existed, a fork run,
parallel teams and a single agent; then the endpoint the page follows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.serve.app import create_app
from airlie.serve.tree import HANDOFF_CHARS, RunNotFound, build_tree, run_tree
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream, cfg

RUN = "20261023-120000-abcd"


def ev(kind: str, ts: float, **fields: Any) -> dict[str, Any]:
    return {"ts": ts, "run_id": RUN, "kind": kind, **fields}


def step(
    team: str, n: int, ts: float, *, tool: str = "read_file", ok: bool = True, role: str = "coder"
) -> dict:
    return ev(
        "step",
        ts,
        team_id=team,
        step=n,
        tool=tool,
        ok=ok,
        prompt_tokens=1000,
        completion_tokens=100,
        model="qwen-coder",
        role=role,
        topology="studio",
    )


def handoff(src: str, dst: str, what: str, text: str, ts: float, **more: Any) -> dict[str, Any]:
    return ev("handoff", ts, src=src, dst=dst, what=what, text=text, **more)


STUDIO_START = [
    ev("run_start", 1.0, goal="fix the build", k=1, repo="/r", orchestrator="studio"),
    ev("plan", 2.0, assumptions=[], subtasks=["fix", "note"], roles=["coder", "docs"], pinned=False),
    ev("route", 2.1, subtask="fix", chosen="coder", decision="hint", confidence=0.9),
    ev("skill", 2.2, subtask="fix", chosen="python", decision="pick", confidence=0.8),
    handoff("chief", "fix", "brief", "make the check pass", 2.3),
    ev("subtask_start", 2.3, subtask="fix", role="coder", skill="python", writes=True),
    step("fix", 1, 3.0),
    step("fix", 2, 4.0, tool="write_file"),
]
"""A studio run part way: planned two subtasks, the coder on its second step, the docs subtask waiting."""

STUDIO_DONE = [
    *STUDIO_START,
    step("fix", 3, 5.0, tool="run_tests"),
    handoff("fix", "reviewer", "diff", "1 file changed: PASS", 6.0, role="reviewer"),
    ev("review", 7.0, subtask="fix", verdict="request_changes", findings=1, blocking=1, round=0),
    handoff("reviewer", "fix", "findings", "request_changes: PASS: wrong content", 7.0, role="reviewer"),
    ev("fix_round", 7.1, subtask="fix", round=1, blocking=1),
    step("fix", 1, 8.0, tool="write_file"),
    handoff("fix", "reviewer", "diff", "1 file changed: PASS", 9.0, role="reviewer"),
    ev("review", 10.0, subtask="fix", verdict="approve", findings=0, blocking=0, round=1),
    handoff("reviewer", "fix", "findings", "approve: no findings", 10.0, role="reviewer"),
    ev("subtask_done", 10.1, subtask="fix", role="coder", ok=True, stop_reason="done", steps=4, fix_rounds=1),
    handoff("fix", "chief", "result", "wrote PASS; the check passes", 10.1),
    ev("route", 10.2, subtask="note", chosen="docs", decision="hint", confidence=0.9),
    handoff("chief", "note", "brief", "describe the repo", 10.3),
    handoff("fix", "note", "result", "wrote PASS; the check passes", 10.3),
    ev("subtask_start", 10.3, subtask="note", role="docs", skill=None, writes=False),
    step("note", 1, 11.0, role="docs"),
    ev("subtask_done", 12.0, subtask="note", role="docs", ok=True, stop_reason="done", steps=1, fix_rounds=0),
    handoff("note", "chief", "result", "the repo holds a check", 12.0),
    ev("synthesis", 13.0, chars=8, open_questions=[]),
    handoff("chief", "synthesis", "answer", "all done", 13.0),
    ev("run_done", 14.0, ok=True, wall_ms=13000.0, subtasks=2, tests=True),
]
"""The same run to its end: a fix round, the docs subtask fed the coder's result, then synthesis."""


def nodes(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in tree["nodes"]}


def active_edges(tree: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(e["src"], e["dst"], e["what"]) for e in tree["edges"] if e["active"]]


def test_a_running_studio_job_shows_the_chief_its_specialists_and_who_is_working() -> None:
    tree = build_tree(STUDIO_START, models={"chief": "qwen-27b", "reviewer": "muse"})
    assert tree["topology"] == "studio" and tree["state"] == "running" and tree["goal"] == "fix the build"
    n = nodes(tree)
    assert list(n) == ["chief", "fix", "note", "reviewer"]  # chief, the plan's subtasks in order, reviewer
    assert n["chief"]["state"] == "idle" and n["chief"]["model"] == "qwen-27b"  # its specialists work now
    fix = n["fix"]
    assert fix["state"] == "active" and fix["label"] == "Coder" and fix["role"] == "coder"
    assert fix["skill"] == "python" and fix["model"] == "qwen-coder"
    assert fix["steps"] == 2 and fix["tokens"] == {"prompt": 2000, "completion": 200}
    assert fix["current"] == {"step": 2, "tool": "write_file", "ok": True}
    assert fix["recent"][-1] == "step 2 · write_file"
    assert n["note"]["state"] == "waiting" and n["note"]["role"] == "docs"  # planned, not routed yet
    assert n["reviewer"]["state"] == "waiting" and n["reviewer"]["model"] == "muse"
    assert active_edges(tree) == [("chief", "fix", "brief")]
    brief = tree["edges"][0]
    assert brief["text"] == "make the check pass" and brief["ts"] == 2.3


def test_the_chief_plans_first_then_the_reviewer_works_while_its_subtask_waits() -> None:
    planning = build_tree(STUDIO_START[:1])
    assert [(n["id"], n["state"]) for n in planning["nodes"]] == [("chief", "active")]
    reviewing = build_tree([*STUDIO_START, handoff("fix", "reviewer", "diff", "1 file changed", 6.0)])
    n = nodes(reviewing)
    assert n["reviewer"]["state"] == "active" and n["fix"]["state"] == "idle"
    assert active_edges(reviewing) == [("fix", "reviewer", "diff")]


def test_a_finished_studio_job_shows_every_hand_off_and_nothing_working() -> None:
    tree = build_tree(STUDIO_DONE)
    assert tree["state"] == "done" and tree["ok"] is True
    n = nodes(tree)
    assert list(n) == ["chief", "fix", "note", "reviewer", "synthesis"]
    assert {k: v["state"] for k, v in n.items()} == dict.fromkeys(n, "done")
    assert n["fix"]["steps"] == 4 and n["fix"]["fix_rounds"] == 1 and n["fix"]["current"] is None
    assert n["synthesis"]["label"] == "Synthesis"
    assert active_edges(tree) == []
    assert [(e["src"], e["dst"], e["what"]) for e in tree["edges"]] == [
        ("chief", "fix", "brief"),
        ("fix", "reviewer", "diff"),
        ("reviewer", "fix", "findings"),
        ("fix", "reviewer", "diff"),
        ("reviewer", "fix", "findings"),
        ("fix", "chief", "result"),
        ("chief", "note", "brief"),
        ("fix", "note", "result"),
        ("note", "chief", "result"),
        ("chief", "synthesis", "answer"),
    ]
    assert "fix round 1 (1 blocking)" in n["fix"]["recent"]
    assert n["reviewer"]["recent"][-1] == "fix: approve, 0 findings (round 1)"


def test_a_fix_round_makes_the_subtask_work_again_on_the_reviewers_findings() -> None:
    upto = STUDIO_DONE.index(ev("fix_round", 7.1, subtask="fix", round=1, blocking=1)) + 1
    tree = build_tree(STUDIO_DONE[:upto])
    n = nodes(tree)
    assert n["fix"]["state"] == "active" and n["reviewer"]["state"] == "idle"
    assert active_edges(tree) == [("reviewer", "fix", "findings")]


def test_a_studio_run_logged_before_hand_offs_gets_its_edges_from_what_it_did_log() -> None:
    old = [e for e in STUDIO_DONE if e["kind"] != "handoff"]
    tree = build_tree(old)
    assert [(e["src"], e["dst"], e["what"], e["text"]) for e in tree["edges"]] == [
        ("chief", "fix", "brief", None),
        ("fix", "reviewer", "diff", None),
        ("reviewer", "fix", "findings", "request_changes: 1 finding, 1 blocking"),
        ("fix", "reviewer", "diff", None),
        ("reviewer", "fix", "findings", "approve: 0 findings, 0 blocking"),
        ("fix", "chief", "result", "done after 4 steps"),
        ("chief", "note", "brief", None),
        ("note", "chief", "result", "done after 1 step"),
        ("chief", "synthesis", "answer", "8 characters, 0 open questions"),
    ]


def test_a_failed_subtask_and_a_failed_run_say_so() -> None:
    tree = build_tree(
        [
            *STUDIO_START,
            ev("subtask_done", 5.0, subtask="fix", role="coder", ok=False, stop_reason="max_steps", steps=2),
            ev("run_done", 6.0, ok=False, wall_ms=5000.0, subtasks=1, tests=False),
        ]
    )
    n = nodes(tree)
    assert tree["state"] == "done" and tree["ok"] is False
    assert n["fix"]["state"] == "failed" and n["fix"]["stop_reason"] == "max_steps"
    assert n["note"]["state"] == "waiting"  # never ran
    assert "reviewer" not in n  # the run ended without one


def test_a_plan_only_job_is_the_chief_and_the_subtasks_it_planned() -> None:
    tree = build_tree(
        [
            ev("run_start", 1.0, goal="g", k=1, repo="/r", orchestrator="studio", mode="plan"),
            ev("plan", 2.0, subtasks=["a"], roles=["coder"]),
            ev("run_done", 3.0, ok=True, wall_ms=2000.0, subtasks=0, tests=None),
        ]
    )
    assert tree["mode"] == "plan" and [n["id"] for n in tree["nodes"]] == ["chief", "a"]
    assert nodes(tree)["a"]["state"] == "waiting"


def test_a_long_hand_off_is_cut_to_what_the_page_shows() -> None:
    tree = build_tree([*STUDIO_START[:4], handoff("chief", "fix", "brief", "x" * 2000, 2.3)])
    assert len(tree["edges"][0]["text"]) == HANDOFF_CHARS


FORK = [
    ev("run_start", 1.0, goal="g", topology="fork", repo="/r", width=2),
    ev("branch_start", 1.1, branch="b0", worktree="/w/b0", child=False),
    step("b0", 1, 2.0),
    ev("branch_done", 3.0, branch="b0", ok=False, stop_reason="stuck", steps=1, prompt_tokens=1000),
    ev(
        "fork_point",
        3.1,
        round=1,
        parent="b0",
        reason="stuck",
        children=["b1", "b2"],
        hypotheses=["the parser is wrong", "the test is wrong"],
    ),
    ev("branch_start", 3.2, branch="b1", worktree="/w/b1", child=True),
    ev("branch_start", 3.2, branch="b2", worktree="/w/b2", child=True),
    step("b1", 1, 4.0, tool="write_file"),
]


def test_a_fork_run_shows_the_stuck_agent_and_its_forks_each_with_its_hypothesis() -> None:
    tree = build_tree(FORK)
    assert tree["topology"] == "fork"
    n = nodes(tree)
    assert list(n) == ["b0", "b1", "b2"]
    assert n["b0"]["state"] == "stuck" and n["b0"]["label"] == "Chief"
    assert n["b1"]["state"] == "active" and n["b1"]["label"] == "Fork 1"
    assert n["b2"]["state"] == "active" and n["b2"]["current"] is None  # started, no step yet
    assert [(e["src"], e["dst"], e["text"], e["active"]) for e in tree["edges"]] == [
        ("b0", "b1", "the parser is wrong", True),
        ("b0", "b2", "the test is wrong", True),
    ]
    done = build_tree(
        [
            *FORK,
            ev("branch_done", 5.0, branch="b1", ok=True, stop_reason="done", steps=1),
            ev("branch_done", 5.0, branch="b2", ok=False, stop_reason="cancelled", steps=0),
            ev("fork_resolved", 5.1, round=1, winner="b1", how="tests"),
            ev("run_done", 6.0, ok=True, wall_ms=5000.0, forks=2, rounds=1, winner="b1"),
        ]
    )
    n = nodes(done)
    assert n["b1"]["won"] is True and n["b1"]["state"] == "done" and n["b2"]["state"] == "failed"
    assert active_edges(done) == []


def test_parallel_teams_and_a_single_agent() -> None:
    teams = build_tree(
        [
            ev("run_start", 1.0, goal="g", k=2, repo="/r", orchestrator="native"),
            ev("team_start", 1.1, team_id="t0", worktree="/w/0"),
            ev("team_start", 1.1, team_id="t1", worktree="/w/1"),
            step("t0", 1, 2.0),
            ev("team_done", 3.0, team_id="t1", ok=True, stop_reason="done", steps=3),
        ]
    )
    assert teams["topology"] == "teams"
    n = nodes(teams)
    assert [(k, v["label"], v["state"]) for k, v in n.items()] == [
        ("t0", "Team 1", "active"),
        ("t1", "Team 2", "done"),
    ]
    judged = build_tree(
        [
            ev("run_start", 1.0, goal="g", k=2, repo="/r", orchestrator="native"),
            ev("team_start", 1.1, team_id="t0", worktree="/w/0"),
            ev("team_start", 1.1, team_id="t1", worktree="/w/1"),
            ev("team_done", 3.0, team_id="t0", ok=False, stop_reason="max_steps", steps=3),
            ev("team_done", 3.0, team_id="t1", ok=True, stop_reason="done", steps=3),
            ev("aggregate", 4.0, winner="t1", reason="tests pass", scores={"t0": 0.0, "t1": 1.0}),
            ev("run_done", 5.0, ok=True, wall_ms=4000.0),
        ]
    )
    n = nodes(judged)
    assert list(n) == ["t0", "t1", "aggregate"] and n["t1"]["won"] is True
    assert [(e["src"], e["dst"], e["text"]) for e in judged["edges"]] == [
        ("t0", "aggregate", "score 0.0"),
        ("t1", "aggregate", "score 1.0"),
    ]
    single = build_tree(
        [
            ev("run_start", 1.0, goal="g", k=1, repo="/r", orchestrator="native"),
            ev("team_start", 1.1, team_id="t0", worktree="/w/0"),
        ]
    )
    assert single["topology"] == "single" and [n["label"] for n in single["nodes"]] == ["Agent"]


def test_no_events_yet_is_an_empty_tree_still_running() -> None:
    tree = build_tree([])
    assert tree["state"] == "running" and tree["nodes"] == [] and tree["edges"] == []


# ---- reading a run directory, and the endpoint ----


def write_run(state: Path, run: str, events: list[dict[str, Any]]) -> None:
    d = state / "runs" / run
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def test_run_tree_reads_a_run_directory_and_counts_its_lines(tmp_path: Path) -> None:
    write_run(tmp_path, RUN, STUDIO_START)
    tree = run_tree(tmp_path, RUN)
    assert tree["run_id"] == RUN and tree["next"] == len(STUDIO_START)
    with pytest.raises(RunNotFound):
        run_tree(tmp_path, "nope")
    with pytest.raises(RunNotFound):
        run_tree(tmp_path, "../stories")


def client_for(tmp_path: Path) -> tuple[TestClient, Path]:
    config = cfg(tmp_path)
    state = config.observability.state_dir.expanduser()
    return TestClient(create_app(config, transport=httpx.MockTransport(FakeUpstream()))), state


def test_the_tree_endpoint_serves_a_run_and_404s_an_unknown_one(tmp_path: Path) -> None:
    client, state = client_for(tmp_path)
    write_run(state, RUN, STUDIO_DONE)
    r = client.get(f"/v1/airlie/runs/{RUN}/tree")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "done" and body["job"] is None  # not a job of this server: a finished run dir
    assert [n["id"] for n in body["nodes"]] == ["chief", "fix", "note", "reviewer", "synthesis"]
    assert client.get("/v1/airlie/runs/nope/tree").status_code == 404


def test_the_tree_endpoint_answers_at_once_when_the_log_grew_and_after_the_wait_when_not(
    tmp_path: Path,
) -> None:
    client, state = client_for(tmp_path)
    write_run(state, RUN, STUDIO_START)
    grown = client.get(f"/v1/airlie/runs/{RUN}/tree", params={"since": 2, "wait": 30})
    assert grown.status_code == 200 and grown.json()["next"] == len(STUDIO_START)
    same = client.get(f"/v1/airlie/runs/{RUN}/tree", params={"since": len(STUDIO_START), "wait": 0.3})
    assert same.status_code == 200 and same.json()["next"] == len(STUDIO_START)
