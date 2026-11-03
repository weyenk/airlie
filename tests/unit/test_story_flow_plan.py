"""The plan lane of a story's flow: a box per step from intake to PLANNED, each with its shape, its live
inner step and the hover card the workflow screen draws."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airlie.serve.flow import story_flow
from tests.unit.test_story_flow import KEY, by_id, write_events

PLAN_IDS = ["intake", "base", "gate", "survey", "refine", "layers", "amigos", "planned"]
SHA = "e8146e0aa11bb22cc33dd44ee55ff66778899aab"


def make_story(root: Path, events: list[dict[str, Any]], **state: Any) -> Path:
    """Story KEY under `root` with `state` over a blank one at stage `new`, and `events` as its log."""
    story = root / "stories" / KEY
    story.mkdir(parents=True)
    base: dict[str, Any] = {"card": {"key": KEY, "title": "t"}, "repo": "/r", "stage": "new", "layers": []}
    (story / "state.json").write_text(json.dumps({**base, **state}))
    write_events(story / "events.jsonl", events)
    return root


def nodes_of(root: Path) -> list[dict[str, Any]]:
    return story_flow(root, KEY)["nodes"]


def turn(ts: float, role: str, n: int, verdict: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "ts": ts,
        "kind": "amigos_turn",
        "role": role,
        "turn": n,
        "verdict": verdict,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "wall_ms": 5000,
    }


def agree() -> dict[str, str]:
    return {"requirements": "agree", "changes": "agree", "proof": "agree"}


def test_the_plan_lane_runs_intake_base_gate_survey_refine_layers_amigos_planned(tmp_path: Path) -> None:
    make_story(tmp_path, [{"ts": 1.0, "kind": "story_start", "stage": "new"}])
    nodes = nodes_of(tmp_path)
    assert [n["id"] for n in nodes][:8] == PLAN_IDS
    assert {n["id"]: n["lane"] for n in nodes[:8]} == dict.fromkeys(PLAN_IDS, "plan")
    assert by_id(nodes, "survey")["group"] == by_id(nodes, "refine")["group"] == "same-time"
    shapes = [by_id(nodes, i)["jobs"][0]["shape"] for i in ("intake", "base", "gate")]
    assert shapes == ["code"] * 3
    assert by_id(nodes, "planned")["jobs"][0]["shape"] == "milestone"
    assert by_id(nodes, "refine")["jobs"][0]["shape"] == "model"
    for n in nodes[:8]:
        for j in n["jobs"]:
            assert {"live", "tags", "card"} <= set(j)


def test_intake_and_base_follow_the_story_state(tmp_path: Path) -> None:
    make_story(
        tmp_path,
        [{"ts": 1.0, "kind": "story_start", "stage": "new"}],
        stage="intake",
        base={"ref": "origin/main", "sha": SHA},
    )
    nodes = nodes_of(tmp_path)
    assert by_id(nodes, "intake")["jobs"][0]["status"] == "passed"
    base = by_id(nodes, "base")["jobs"][0]
    assert base["status"] == "passed" and base["sub"] == "origin/main e8146e0"


def test_the_gate_card_lists_its_commands_and_the_agents_md_commit(tmp_path: Path) -> None:
    make_story(
        tmp_path,
        [{"ts": 2.0, "kind": "gate", "agents_md": "29021b8", "base": {"ref": "origin/main", "sha": SHA}}],
        stage="intake",
        base={"ref": "origin/main", "sha": SHA},
        gate={"commands": ["uv sync --frozen", "uv run mypy"], "agents_md": "29021b8"},
    )
    job = by_id(nodes_of(tmp_path), "gate")["jobs"][0]
    assert job["status"] == "passed" and job["card"]["title"] == "Gate"
    rows = job["card"]["rows"]
    assert [r["text"] for r in rows if r["kind"] == "step"] == ["uv sync --frozen", "uv run mypy"]
    assert {"kind": "text", "label": "AGENTS.md", "text": "29021b8"} in rows


def test_a_story_blocked_at_the_gate_shows_the_gate_failed(tmp_path: Path) -> None:
    make_story(
        tmp_path,
        [{"ts": 1.0, "kind": "story_start", "stage": "new"}],
        stage="intake",
        base={"ref": "origin/main", "sha": SHA},
        blocked="no gate",
    )
    assert by_id(nodes_of(tmp_path), "gate")["jobs"][0]["status"] == "failed"


def test_plan_layers_card_shows_rounds_fixes_and_over_budget(tmp_path: Path) -> None:
    events = [
        {"ts": 10.0, "kind": "run", "stage": "layers", "studio_run": "r1"},
        {"ts": 11.0, "kind": "plan_fix", "stage": "layers", "attempt": 1, "error": "e1"},
        {"ts": 12.0, "kind": "plan_fix", "stage": "layers", "attempt": 2, "error": "e2"},
        {"ts": 13.0, "kind": "retry", "stage": "layers"},
        {"ts": 14.0, "kind": "plan_round", "stage": "layers", "round": 1, "layers": 3},
        {"ts": 15.0, "kind": "plan_round_unusable", "stage": "layers", "round": 2, "error": "bad"},
        {"ts": 16.0, "kind": "plan_done", "stage": "layers", "reason": "converged", "rounds": 2},
        {"ts": 17.0, "kind": "budget_exceeded", "stage": "plan"},
    ]
    make_story(tmp_path, events, stage="refine")
    job = by_id(nodes_of(tmp_path), "layers")["jobs"][0]
    rounds = next(r for r in job["card"]["rows"] if r["kind"] == "squares" and r["label"] == "rounds")
    assert rounds["marks"] == ["passed", "failed"]
    fixes = next(r for r in job["card"]["rows"] if r["label"] == "fixes")
    assert fixes["marks"] == ["warn", "warn", "warn"]
    assert {"text": "over budget", "tone": "warn"} in job["tags"]
    assert job["status"] == "passed"  # plan_done ends it


def test_the_meeting_is_its_own_box_running_from_its_start(tmp_path: Path) -> None:
    events = [
        {"ts": 50.0, "kind": "amigos_start", "resumed": False},
        turn(55.0, "product", 1, agree()),
    ]
    make_story(tmp_path, events, stage="refine")
    job = by_id(nodes_of(tmp_path), "amigos")["jobs"][0]
    assert job["id"] == "meeting" and job["status"] == "running" and job["live"] == "architect"
    assert job["shape"] == "model" and job["started"] == 50.0
    write_events(
        tmp_path / "stories" / KEY / "events.jsonl",
        [
            *events,
            turn(60.0, "architect", 2, agree()),
            turn(65.0, "tester", 3, agree()),
            turn(66.0, "airlie", 4),
            {"ts": 70.0, "kind": "amigos_agreed", "turns": 4},
        ],
    )
    job = by_id(nodes_of(tmp_path), "amigos")["jobs"][0]
    assert job["status"] == "passed" and job["live"] is None
    rows = job["card"]["rows"]
    turns = next(r for r in rows if r["kind"] == "squares")
    assert turns["marks"] == ["passed", "passed", "passed", "code-passed"]
    votes_row = next(r for r in rows if r["kind"] == "votes")
    assert votes_row["votes"][0] == {
        "role": "product",
        "requirements": "agree",
        "changes": "agree",
        "proof": "agree",
    }
    assert [v["role"] for v in votes_row["votes"]] == ["product", "architect", "tester"]
    assert {"kind": "text", "label": "skill", "text": "none"} in rows


def test_an_old_meeting_without_a_start_runs_from_its_skill_pick(tmp_path: Path) -> None:
    skill = {"ts": 40.0, "kind": "skill", "subtask": "amigos", "chosen": "discuss", "decision": "d"}
    make_story(tmp_path, [skill], stage="refine")
    job = by_id(nodes_of(tmp_path), "amigos")["jobs"][0]
    assert job["status"] == "running" and job["live"] == "product" and job["started"] == 40.0
    assert {"kind": "text", "label": "skill", "text": "discuss"} in job["card"]["rows"]


def test_a_meeting_the_story_stopped_in_is_failed(tmp_path: Path) -> None:
    make_story(
        tmp_path,
        [{"ts": 50.0, "kind": "amigos_start", "resumed": False}, turn(55.0, "product", 1)],
        stage="refine",
        blocked="the engine went away",
    )
    job = by_id(nodes_of(tmp_path), "amigos")["jobs"][0]
    assert job["status"] == "failed" and job["live"] is None


def test_no_survey_and_no_meeting_draw_no_boxes_but_planned_still_shows(tmp_path: Path) -> None:
    make_story(tmp_path, [], stage="layers", agreement={"turns": 0, "proof": []})
    nodes = nodes_of(tmp_path)
    assert by_id(nodes, "survey")["jobs"] == [] and by_id(nodes, "amigos")["jobs"] == []
    assert by_id(nodes, "planned")["jobs"][0]["status"] == "passed"


def test_planned_passes_on_its_event_and_carries_the_time(tmp_path: Path) -> None:
    make_story(tmp_path, [{"ts": 90.0, "kind": "story_planned", "layers": 2}], stage="layers")
    job = by_id(nodes_of(tmp_path), "planned")["jobs"][0]
    assert job["status"] == "passed" and job["ended"] == 90.0


def test_an_old_finished_story_with_no_base_or_gate_draws_base_not_recorded_and_no_gate(
    tmp_path: Path,
) -> None:
    make_story(tmp_path, [{"ts": 1.0, "kind": "story_start", "stage": "new"}], stage="review")
    nodes = nodes_of(tmp_path)
    base = by_id(nodes, "base")["jobs"][0]
    assert base["status"] == "passed" and base["sub"] == "not recorded"
    assert by_id(nodes, "gate")["jobs"] == []


def test_a_story_at_intake_with_no_base_yet_shows_base_running(tmp_path: Path) -> None:
    make_story(tmp_path, [{"ts": 1.0, "kind": "story_start", "stage": "new"}], stage="intake")
    assert by_id(nodes_of(tmp_path), "base")["jobs"][0]["status"] == "running"


def test_plan_layers_runs_while_its_run_is_out_and_passes_on_plan_done_before_the_stage_moves(
    tmp_path: Path,
) -> None:
    run = {"ts": 10.0, "kind": "run", "stage": "layers", "studio_run": "r1"}
    make_story(tmp_path, [run], stage="refine")
    assert by_id(nodes_of(tmp_path), "layers")["jobs"][0]["status"] == "running"
    write_events(
        tmp_path / "stories" / KEY / "events.jsonl",
        [run, {"ts": 20.0, "kind": "plan_done", "stage": "layers", "reason": "converged", "rounds": 1}],
    )
    assert by_id(nodes_of(tmp_path), "layers")["jobs"][0]["status"] == "passed"
