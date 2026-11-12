"""The flow of a story as the pipeline page draws it: stages left to right, a lane of jobs per layer, each job
with its status, attempts, tokens and speed, derived from the story's state.json and events and the runs they
name."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from airlie.bench.trace_diff import TIMING, diff_events, diff_prompts
from airlie.config import load_config
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.flow.catalog import STORY_CATALOG, Catalog, Check
from airlie.flow.composer import FAIL_KEY, Composer, EndRun, Registry, RunContext
from airlie.flow.outputs import OutputSpec
from airlie.flow.prompt_files import rule_ids
from airlie.flow.runners import (
    Hold,
    Session,
    ask_runner,
    check_runner,
    conversation_runner,
    gate_runner,
    meeting_runner,
    write_runner,
)
from airlie.flow.spec import WorkflowSpec
from airlie.flow.store import MemoryStore, Sessions
from airlie.prompts.hub import text as hub_text
from airlie.serve.flow import epic_flow, job_detail, story_flow
from airlie.serve.flow_parts import StoryNotFound
from airlie.story.amigos import opening_message
from airlie.story.asking import StoryBlocked
from airlie.story.board import text_card
from airlie.story.flow import story_registry, story_runners
from airlie.story.model import Layer, Story, StoryError
from airlie.story.phase import phase_path, read_phase
from airlie.story.pipeline import REASON_CHARS, StoryPipeline, _cap, _reason
from airlie.story.state import StoryState, lane_lock, lock_held
from airlie.story.workflows import snapshot
from airlie.topology.studio import StudioOrchestrator, SubtaskRun
from airlie.topology.studio_plan import Subtask
from tests.support.git import git
from tests.support.layer_golden import (
    GOLDEN_LEDGER,
    GOLDEN_NUDGES,
    GOLDEN_STATE,
    GOLDEN_STORY_KINDS,
    GOLDEN_STORY_LAYER,
    GOLDEN_STUDIO,
    build_run,
    clean,
    golden_prompts,
    golden_state,
    layer_engine,
)
from tests.support.story import (
    AGREED_SPLIT,
    B2,
    CUT_THINKING,
    EASY_MEETING,
    LAYERS,
    MEETING,
    MEETING_ROLES,
    NOTE,
    PKG1,
    PKG2,
    PKGS,
    REWORDED_STORY,
    SATISFIED,
    STORY,
    SURVEY_NOTES,
    WORKFLOWS,
    card_of_epic,
    gated,
    happy_engine,
    kinds,
    kinds_all,
    layer,
    neutral,
    package_reply,
    pipeline,
    planned,
    replay_meeting,
    repo_with_levels,
    repo_with_marker_tests,
)
from tests.support.story_epic import fenced
from tests.support.story_flow import (
    KEY,
    bare_state,
    briefs_to,
    by_id,
    delivered_initiative,
    run_context,
    step,
    v2,
    write_epic,
    write_events,
    write_story,
)
from tests.support.story_layer_checks_flow import (
    BROKEN,
    FEATURE,
    FIXED,
    OTHER_RED,
    PYTEST,
    RED,
    STILL_BROKEN,
    calc_repo,
    write,
)
from tests.support.story_layer_checks_flow import LAYERS as CALC_LAYERS
from tests.support.studio import APPROVE, CHANGES, DONE, RUN, WRITE_PASS, cfg_for
from tests.support.supervisor_flow import SUPERVISOR_ROLES, answer, rows


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return write_story(tmp_path)


def test_the_flow_lists_the_stages_in_order_with_their_status(state_dir: Path) -> None:
    flow = story_flow(state_dir, KEY, held=True)
    assert flow["key"] == KEY and flow["title"] == "Do the thing"
    assert [n["id"] for n in flow["nodes"]] == [
        "intake", "base", "gate", "survey", "refine", "layers", "amigos", "planned",
        "packages", "layer-01", "layer-02", "publish", "review",
    ]  # fmt: skip
    status = {n["id"]: n["status"] for n in flow["nodes"]}
    assert status["intake"] == status["refine"] == status["layers"] == status["layer-01"] == "passed"
    assert status["layer-02"] == "failed" and status["publish"] == status["review"] == "pending"
    assert status["survey"] == "skipped"  # it never ran


def test_the_acceptance_criteria_and_the_block_reason_come_with_the_flow(state_dir: Path) -> None:
    flow = story_flow(state_dir, KEY, held=True)
    assert flow["acceptance"] == [{"id": "AC1", "given": "g", "when": "w", "then": "t"}]
    assert flow["blocked"].startswith("layer 02")


def test_a_stage_job_counts_its_retries_its_time_and_its_tokens(state_dir: Path) -> None:
    refine = by_id(story_flow(state_dir, KEY, held=True)["nodes"], "refine")
    (job,) = refine["jobs"]
    assert job["role"] is not None and job["attempts"] == 2 and job["studio_run"] == "run-refine"
    assert job["retries"] == [{"kind": "retry", "detail": "no acceptance criteria"}]
    assert (
        job["tokens"]["prompt"] == 2000 and job["tokens"]["completion"] == 200 and job["tokens"]["steps"] == 2
    )
    assert job["tokens"]["tps"] == pytest.approx(50.0)  # 200 tokens in 4 s of generation
    # the engine's generation time can read far too small, so the whole turn is timed too: 200 tokens in 5 s
    assert job["tokens"]["effective_tps"] == pytest.approx(40.0)
    assert job["tokens"]["prefill_tps"] == pytest.approx(2000.0)  # 2000 prompt tokens in 1 s of prefill
    assert job["duration_s"] == pytest.approx(28.0)  # from its run to the stage finishing


def test_a_story_recorded_before_the_package_still_renders_its_tests_job(state_dir: Path) -> None:
    """A story built with a tester (before P1, 2026-10-24) has a `tests` subtask in its layer runs and no
    package events: its lane is the tester's, as it was recorded."""
    layer = by_id(story_flow(state_dir, KEY, held=True)["nodes"], "layer-01")
    assert layer["title"] == "The seam" and layer["acceptance"] == ["AC1"]
    assert [j["id"] for j in layer["jobs"]] == ["tests", "checks", "build"]
    tests, checks, build = layer["jobs"]
    assert tests["role"] == "tester" and tests["status"] == "passed"
    assert tests["tokens"]["tps"] == pytest.approx(50.0)
    assert checks["status"] == "passed" and checks["attempts"] == 1
    assert build["role"] == "coder" and build["status"] == "passed" and build["fix_rounds"] == 1


def test_a_layer_that_stopped_shows_which_job_failed_and_why(state_dir: Path) -> None:
    layer = by_id(story_flow(state_dir, KEY, held=True)["nodes"], "layer-02")
    tests, checks, build = layer["jobs"]
    assert tests["status"] == "passed"
    assert checks["status"] == "failed" and checks["attempts"] == 2
    assert [r["kind"] for r in checks["retries"]] == ["no_new_test", "test_checks_retry", "no_new_test"]
    assert build["status"] == "pending"


def test_a_job_detail_gives_its_prompt_its_steps_and_its_reasoning(state_dir: Path) -> None:
    detail = job_detail(state_dir, KEY, "layer-01", "tests")
    assert detail["input"]["messages"][0]["content"] == "write tests"
    assert detail["steps"][0]["tool"] == "read_file" and detail["steps"][0]["reasoning"] == "think"
    assert detail["output"] is not None


def test_an_old_run_without_a_recorded_prompt_says_so(state_dir: Path) -> None:
    detail = job_detail(state_dir, KEY, "layer-01", "build")
    assert detail["input"] is None and detail["input_note"] == "opening prompt not recorded"


def test_a_story_that_is_not_there_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(StoryNotFound):
        story_flow(tmp_path, "AIR-404")


def test_a_layer_run_without_a_tester_shows_its_tests_job_as_skipped(state_dir: Path) -> None:
    write_events(
        state_dir / "runs" / "run-l2" / "events.jsonl",
        [
            {"ts": 301.0, "kind": "subtask_start", "subtask": "build", "role": "coder"},
            {
                "ts": 340.0,
                "kind": "subtask_done",
                "subtask": "build",
                "role": "coder",
                "ok": False,
                "stop_reason": "max_steps",
                "steps": 3,
                "fix_rounds": 0,
            },
        ],
    )
    tests, _, build = by_id(story_flow(state_dir, KEY, held=True)["nodes"], "layer-02")["jobs"]
    assert tests["status"] == "skipped" and build["status"] == "failed"


KEY2 = "AIR-2"


def write_packaged_story(root: Path) -> Path:
    """Story KEY2 built after P1: layer 01 got its package at the first try and was built, layer 02's package
    came back twice (a test that passes before the code) and stopped it. Returns `root`."""
    story = root / "stories" / KEY2
    story.mkdir(parents=True)
    layers = [
        {"slug": "seam", "title": "The seam", "scope": "src/a.py", "brief": "b", "acceptance": ["AC1"]},
        {"slug": "wire", "title": "Wire it", "scope": "src/b.py", "brief": "w", "acceptance": ["AC2"]},
    ]
    state = {
        "card": {"key": KEY2, "title": "Do the other thing"},
        "repo": "/r",
        "stage": "layers",
        "story": {"title": "Do the other thing", "summary": "s", "acceptance": []},
        "layers": layers,
        "built": {"01": {"branch": "b1", "sha": "abc", "run_id": "run-b1"}},
        "blocked": "layer 02 (Wire it): its package still fails the readiness check after 2 tries",
        "runs": ["run-p1", "run-b1", "run-p2a", "run-p2b"],
    }
    (story / "state.json").write_text(json.dumps(state))
    write_events(
        story / "events.jsonl",
        [
            {"ts": 10.0, "kind": "story_start", "stage": "new"},
            {"ts": 11.0, "kind": "stage", "stage": "layers"},
            {"ts": 20.0, "kind": "package_start", "layer": "01", "attempt": 1},
            {"ts": 21.0, "kind": "run", "stage": "package", "studio_run": "run-p1"},
            {"ts": 80.0, "kind": "package_ready", "layer": "01", "attempt": 1, "studio_run": "run-p1"},
            {"ts": 81.0, "kind": "layer_start", "layer": "01", "studio_run": "run-b1", "attempt": 1},
            {"ts": 82.0, "kind": "package_placed", "layer": "01", "paths": ["tests/test_a.py"]},
            {"ts": 200.0, "kind": "layer_built", "layer": "01", "branch": "b1", "sha": "abc"},
            {"ts": 201.0, "kind": "package_start", "layer": "02", "attempt": 1},
            {"ts": 220.0, "kind": "passes_before_code", "layer": "02", "attempt": 1, "test": "t::x"},
            {
                "ts": 221.0,
                "kind": "package_returned",
                "layer": "02",
                "attempt": 1,
                "kinds": ["passes_before_code"],
                "studio_run": "run-p2a",
            },
            {"ts": 222.0, "kind": "package_start", "layer": "02", "attempt": 2},
            {"ts": 240.0, "kind": "import_unresolved", "layer": "02", "attempt": 2, "test": "t: calc.div"},
            {
                "ts": 241.0,
                "kind": "package_returned",
                "layer": "02",
                "attempt": 2,
                "kinds": ["import_unresolved"],
                "studio_run": "run-p2b",
            },
            {"ts": 242.0, "kind": "blocked", "stage": "layers", "reason": "layer 02 stopped"},
        ],
    )
    runs = root / "runs"
    write_events(
        runs / "run-p1" / "events.jsonl", [step("run-p1", "package", 1, completion=300, gen_ms=6000.0)]
    )
    write_events(
        runs / "run-b1" / "events.jsonl",
        [
            {"ts": 81.0, "kind": "subtask_start", "subtask": "build", "role": "coder"},
            step("run-b1", "build", 1),
            {
                "ts": 190.0,
                "kind": "subtask_done",
                "subtask": "build",
                "role": "coder",
                "ok": True,
                "stop_reason": "done",
                "steps": 1,
                "fix_rounds": 0,
            },
        ],
    )
    trace = runs / "run-p1" / "trace" / "package"
    trace.mkdir(parents=True)
    (trace / "prompt.json").write_text(
        json.dumps({"role": "architect", "attempt": 0, "messages": [{"role": "user", "content": "package"}]})
    )
    return root


def test_a_layer_is_a_lane_of_jobs_with_package_and_coder_stats(state_dir: Path) -> None:
    write_packaged_story(state_dir)
    layer = by_id(story_flow(state_dir, KEY2, held=True)["nodes"], "layer-01")
    assert [j["id"] for j in layer["jobs"]] == ["package", "checks", "place", "build", "squash"]
    package, checks, place, build, squash = layer["jobs"]
    assert (package["role"], package["status"], package["attempts"]) == ("architect", "passed", 1)
    assert package["studio_run"] == "run-p1" and package["tokens"]["tps"] == pytest.approx(50.0)
    assert package["duration_s"] == pytest.approx(60.0)  # from package_start to package_ready
    assert checks["status"] == "passed" and checks["retries"] == [] and checks["label"] == "checks"
    assert place["status"] == "passed" and place["sub"] == "1 test file"
    assert build["role"] == "coder" and build["status"] == "passed"
    assert squash["status"] == "passed"


def test_a_layer_whose_package_came_back_twice_shows_the_package_job_failed(state_dir: Path) -> None:
    write_packaged_story(state_dir)
    layer = by_id(story_flow(state_dir, KEY2, held=True)["nodes"], "layer-02")
    package, checks, place, build, squash = layer["jobs"]
    assert (package["status"], package["attempts"]) == ("failed", 2)
    assert checks["status"] == "failed" and checks["attempts"] == 2
    assert [r["kind"] for r in checks["retries"]] == [
        "passes_before_code",
        "package_returned",
        "import_unresolved",
        "package_returned",
    ]
    assert place["status"] == build["status"] == squash["status"] == "pending"
    assert layer["status"] == "failed"


def test_a_package_that_is_being_written_is_running(state_dir: Path) -> None:
    write_packaged_story(state_dir)
    events = state_dir / "stories" / KEY2 / "events.jsonl"
    lines = events.read_text().splitlines()
    events.write_text("\n".join(lines[:-3]) + "\n")  # layer 02: attempt 1 returned, attempt 2 started
    state = json.loads((state_dir / "stories" / KEY2 / "state.json").read_text())
    state["blocked"] = None
    (state_dir / "stories" / KEY2 / "state.json").write_text(json.dumps(state))
    package, *_ = by_id(story_flow(state_dir, KEY2, held=True)["nodes"], "layer-02")["jobs"]
    assert package["status"] == "running"


def test_the_package_job_detail_gives_the_architects_prompt(state_dir: Path) -> None:
    write_packaged_story(state_dir)
    detail = job_detail(state_dir, KEY2, "layer-01", "package")
    assert detail["input"]["messages"][0]["content"] == "package"


# ---- an epic (P3, 2026-10-24) ----


def test_the_epic_flow_lists_its_plan_its_stories_and_where_each_stands(tmp_path: Path) -> None:
    write_epic(tmp_path)
    flow = epic_flow(tmp_path, "AIR-1", statuses={"AIR-2": "review", "AIR-3": "todo"})
    assert (flow["key"], flow["title"], flow["stage"], flow["blocked"]) == (
        "AIR-1",
        "Avatars",
        "building",
        None,
    )
    assert flow["summary"] == "Avatars, in two stories." and flow["constraints"] == ["PNG only"]
    assert [c["id"] for c in flow["criteria"]] == ["EC1", "EC2"]
    first, second = flow["stories"]
    assert (first["id"], first["key"], first["status"], first["stage"], first["size"]) == (
        "S1",
        "AIR-2",
        "review",
        "review",
        "S",
    )
    assert (second["id"], second["depends_on"], second["epic_criteria"]) == ("S2", ["S1"], ["EC2"])
    assert (second["status"], second["stage"]) == ("todo", None)  # no state yet: it has not started
    assert flow["left_out"] == ["AIR-9"] and flow["replans"][0]["reason"] == "AIR-9 stopped again"
    assert flow["risks"] == [{"risk": "a huge file", "pinned": "S2/AC1"}]


def test_an_epic_flow_without_board_statuses_says_none(tmp_path: Path) -> None:
    write_epic(tmp_path)
    assert [s["status"] for s in epic_flow(tmp_path, "AIR-1")["stories"]] == [None, None]


def test_an_epic_that_is_not_there_or_has_no_safe_key_is_not_found(tmp_path: Path) -> None:
    write_epic(tmp_path)
    for key in ("NOPE-1", "../stories", "a/b"):
        with pytest.raises(StoryNotFound):
            epic_flow(tmp_path, key)


def test_an_epic_with_a_damaged_record_is_not_found_with_the_reason(tmp_path: Path) -> None:
    write_epic(tmp_path, damaged=True)
    with pytest.raises(StoryNotFound, match="damaged"):
        epic_flow(tmp_path, "AIR-1")


def test_an_epic_not_planned_yet_has_no_stories(tmp_path: Path) -> None:
    write_epic(tmp_path)
    path = tmp_path / "epics" / "AIR-1" / "state.json"
    state = json.loads(path.read_text())
    state.update(plan=None, stories={}, stage="blocked", blocked="the epic has no closing JSON block")
    path.write_text(json.dumps(state))
    flow = epic_flow(tmp_path, "AIR-1")
    assert flow["stories"] == [] and flow["blocked"] == "the epic has no closing JSON block"
    assert flow["summary"] is None


# ---- an initiative as the app draws it (D9 of the specs phase, P4 of the planning line, 2026-10-25) ----


def test_the_initiative_flow_shows_the_dialogue_the_consultations_the_epics_and_the_queue(
    tmp_path: Path,
) -> None:
    from airlie.serve.flow import initiative_flow

    store, init, core, ext = delivered_initiative(tmp_path)
    flow = initiative_flow(tmp_path, store, init)
    assert (flow["key"], flow["title"], flow["stage"], flow["blocked"]) == (
        init,
        "Linear feel",
        "delivered",
        None,
    )
    assert flow["waiting_on"] == "person" and flow["question"] == "Who?" and flow["budget_hours"] == 20.0
    assert flow["summary"] == "Make the board fast." and flow["assumptions"] == ["one user"]
    assert flow["qa"][0] == {"q": "What is it for?", "a": "Speed.", "asked_at": 1.0, "answered_at": 2.0}
    assert flow["qa"][1]["a"] is None
    assert flow["consultations"] == [
        {"seat": "ux", "question": "How?", "answer": "- keys", "seconds": 12.3, "round": 1}
    ]
    first, second, third = flow["epics"]
    assert first == {
        "key": core,
        "id": "E1",
        "kind": "core",
        "title": "Fast board",
        "status": "approved",
        "hours": 5.0,
        "basis": "1 M at 5.0 h (default)",
        "revised": 6.0,
        "actual": None,
        "verdict": "risky",
        "options": [
            f"/v1/airlie/initiatives/{init}/mockups/E1/option-1.html",
            f"/v1/airlie/initiatives/{init}/mockups/E1/option-3.html",
        ],
        "picked": 3,
        "depends_on": [],
    }
    assert second["key"] == ext and second["status"] == "review" and second["depends_on"] == [core]
    assert second["hours"] == 2.0 and second["options"] == [] and second["picked"] is None
    assert third["key"] is None and third["status"] is None and third["hours"] is None  # drafted, not carded
    assert flow["queue_hours"] == 6.0  # the approved core, at its revised price, none of it done
    working = initiative_flow(tmp_path, store, init)
    assert working["waiting_on"] == "person"


def test_an_initiative_flow_names_what_the_line_works_on_and_what_is_not_there(tmp_path: Path) -> None:
    from airlie.board import BoardStore
    from airlie.serve.flow import initiative_flow

    store = BoardStore(tmp_path / "board.sqlite")
    folder = tmp_path / "initiatives" / "AIR-7"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(json.dumps({"card": {"title": "t"}, "stage": "imagining", "qa": []}))
    flow = initiative_flow(tmp_path, store, "AIR-7")
    assert flow["waiting_on"] == "line" and flow["question"] is None and flow["epics"] == []
    assert flow["budget_hours"] is None  # the card is not on this board
    (folder / "state.json").write_text("{")
    with pytest.raises(StoryNotFound, match="damaged"):
        initiative_flow(tmp_path, store, "AIR-7")
    for key in ("AIR-99", "../x", ""):
        with pytest.raises(StoryNotFound):
            initiative_flow(tmp_path, store, key)


# ---- the story meeting (P2, 2026-10-24) ----


def add_events(state_dir: Path, events: list[dict[str, Any]]) -> None:
    path = state_dir / "stories" / KEY / "events.jsonl"
    path.write_text(path.read_text() + "".join(json.dumps(e) + "\n" for e in events))


def turn(ts: float, role: str, n: int, prompt: int = 1000, completion: int = 100) -> dict[str, Any]:
    return {
        "ts": ts,
        "kind": "amigos_turn",
        "role": role,
        "turn": n,
        "verdict": None,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }


MEETING_EVENTS = [
    turn(50.0, "product", 1),
    turn(60.0, "architect", 2),
    turn(70.0, "tester", 3),
    {"ts": 71.0, "kind": "amigos_objection", "role": "product", "part": "proof", "about": "AC1", "round": 2},
    {
        "ts": 72.0,
        "kind": "amigos_objection",
        "role": "tester",
        "part": "proof",
        "about": "t::x",
        "source": "package",
    },
    turn(80.0, "airlie", 4, prompt=0, completion=0),
]


def test_a_story_shows_its_meeting_with_turns_and_objections(state_dir: Path) -> None:
    add_events(state_dir, [*MEETING_EVENTS, {"ts": 85.0, "kind": "amigos_agreed", "turns": 4}])
    amigos = by_id(story_flow(state_dir, KEY, held=True)["nodes"], "amigos")
    assert [j["id"] for j in amigos["jobs"]] == ["meeting"]
    meeting = by_id(amigos["jobs"], "meeting")
    assert (meeting["role"], meeting["status"]) == ("amigos", "passed")
    assert (meeting["turns"], meeting["objections"]) == (
        4,
        1,
    )  # a package critic's objection is not the meeting's
    assert meeting["tokens"]["prompt"] == 3000 and meeting["tokens"]["completion"] == 300
    assert meeting["started"] == 50.0 and meeting["ended"] == 85.0 and meeting["duration_s"] == 35.0
    assert meeting["owner_decided"] == []


def test_a_meeting_that_ran_out_of_turns_says_which_parts_their_owners_decided(state_dir: Path) -> None:
    add_events(
        state_dir,
        [
            *MEETING_EVENTS,
            {"ts": 85.0, "kind": "amigos_owner_decided", "parts": ["proof"], "turns": 4},
            {"ts": 85.5, "kind": "amigos_unresolved", "role": "product", "part": "proof", "about": "AC1"},
        ],
    )
    meeting = by_id(by_id(story_flow(state_dir, KEY, held=True)["nodes"], "amigos")["jobs"], "meeting")
    assert (
        meeting["status"] == "passed" and meeting["owner_decided"] == ["proof"] and meeting["unresolved"] == 1
    )


def test_a_meeting_in_progress_is_running_and_one_the_story_stopped_in_is_failed(state_dir: Path) -> None:
    add_events(state_dir, MEETING_EVENTS)
    state_file = state_dir / "stories" / KEY / "state.json"
    state = json.loads(state_file.read_text())
    state.update(stage="refine", blocked=None, built={})
    state_file.write_text(json.dumps(state))
    amigos = by_id(story_flow(state_dir, KEY, held=True)["nodes"], "amigos")
    assert by_id(amigos["jobs"], "meeting")["status"] == "running" and amigos["status"] == "running"
    state["blocked"] = "the engine went away"
    state_file.write_text(json.dumps(state))
    amigos = by_id(story_flow(state_dir, KEY, held=True)["nodes"], "amigos")
    assert by_id(amigos["jobs"], "meeting")["status"] == "failed"


def test_a_story_without_a_meeting_has_no_meeting_job_and_the_job_has_a_detail(state_dir: Path) -> None:
    nodes = story_flow(state_dir, KEY, held=True)["nodes"]
    assert [j["id"] for j in by_id(nodes, "layers")["jobs"]] == ["layers"]
    assert by_id(nodes, "amigos")["jobs"] == []
    add_events(state_dir, [*MEETING_EVENTS, {"ts": 85.0, "kind": "amigos_agreed", "turns": 4}])
    state_file = state_dir / "stories" / KEY / "state.json"
    state = json.loads(state_file.read_text())
    state["agreement"] = {"turns": 4, "proof": []}
    state_file.write_text(json.dumps(state))
    detail = job_detail(state_dir, KEY, "amigos", "meeting")
    assert detail["output"]["agreement"] == {"turns": 4, "proof": []} and "meeting" in detail["input_note"]


def test_the_story_flow_carries_its_diagnosis_and_budgets(tmp_path: Path) -> None:
    """The row the Vue app draws gains the latest block's class and kind, and the per-stage figures the line
    recorded (planning-line P5b, D11)."""
    from airlie.story.diagnosis import Classified, Diagnosis, Pack, Paths, write_diagnosis

    write_story(tmp_path)
    plain = story_flow(tmp_path, KEY, held=True)
    assert plain["diagnosis"] is None and plain["budgets"] == {}  # never diagnosed, nothing recorded
    pack = Pack(
        key=KEY, n=2, reason="x", stage="layers", layer="02", attempt=1, run_id=None, subtask=None, role=None,
        stop_reason=None, refused_calls=0, holds=(), repeats=0, continuations=0, steps=0, tokens=0,
        minutes=0.0, gate_tail="", tracebacks=(), failures=(), base_gate_failed=None, base_failing=(),
        diff_stat="", paths=Paths(tmp_path / "e", None, None, ()),
    )  # fmt: skip
    write_diagnosis(
        tmp_path,
        Diagnosis(
            pack,
            Classified("process", "hold_trap", "layers", "process:hold_trap:tests", ("x",), "r"),
            None,
            "n",
        ),
    )
    stages = tmp_path / "stories" / KEY / "stages"
    stages.mkdir()
    (stages / "layer.json").write_text(json.dumps({"tokens": 5000, "wall_s": 900.0, "state": "ok"}))
    flow = story_flow(tmp_path, KEY, held=True)
    assert flow["diagnosis"] == {
        "class": "process",
        "kind": "hold_trap",
        "signature": "process:hold_trap:tests",
        "n": 2,
    }
    assert flow["budgets"] == {"layer": {"tokens": 5000, "wall_s": 900.0, "state": "ok"}}


def test_the_shipped_workflow_lists_the_diagram_boxes_in_order():
    cfg = load_config(Path("airlie.yaml"))
    assert list(cfg.workflows["story"].nodes) == [
        "intake", "base", "to_build", "gate", "request", "story_card", "take_card_as_story", "refine",
        "survey", "survey_note", "costed_refine", "layers", "costed_plan", "agree_plain", "amigos",
        "costed_amigos", "planned", "build_layers", "built", "publish", "costed_publish", "review",
    ]  # fmt: skip
    layer = cfg.workflows["layer"]
    assert list(layer.nodes) == [
        "start", "covered", "drafts", "package", "place", "build", "done_check", "review", "fix",
        "final_gate", "gate_advice", "follow_up", "regate", "outcome", "squash", "costed_layer",
    ]  # fmt: skip
    assert cfg.workflows["story"].nodes["amigos"].role == ["product", "architect", "tester"]
    [back] = [line for line in layer.nodes["outcome"].to if line.loop is not None]
    assert back.node == "package" and back.loop is not None
    assert (back.loop.max, back.loop.carry) == ("story.layer_attempts", "feedback")
    assert layer.outputs == ["drafted", "built.{item}", "satisfied.{item}"]


# ---- the plan lane through the composer (story.composer), against the driver ----

# captured once from the driver (composer=False): the contract the composer meets
EXPECTED_PLAN_KINDS = ["story_start", "stage", "run", "stage", "run", "stage", "story_planned", "story_done"]


def plan_kinds(path: Path) -> list[str]:
    return [e["kind"] for e in kinds_all(path) if not str(e["kind"]).startswith("node_")]


@pytest.mark.parametrize("composer", [False, True])
async def test_the_plan_lane_produces_the_same_events_and_state_either_way(
    tmp_path: Path, composer: bool
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=composer)
    st = await pipe.plan(text_card("add a marker"))
    assert plan_kinds(st.path / "events.jsonl") == EXPECTED_PLAN_KINDS
    assert st.story is not None and st.layers is not None and st.agreement is not None
    assert st.blocked is None and st.stage == "layers"


async def test_a_handover_after_planning_is_not_a_resume_despite_node_events(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=True)
    st = await pipe.plan(text_card("add a marker"))
    assert any(e["kind"] == "node_started" for e in kinds_all(st.path / "events.jsonl"))
    assert StoryPipeline._handoff(st, "review") is True


VOLATILE = frozenset({"ts", "build", "run_id", "studio_run", "ms", "elapsed_s", "wall_s", "wall_ms"})


def plan_events(path: Path, root: Path, sha: str) -> list[dict[str, Any]]:
    """The story's events without the composer's node events and without what differs run to run."""
    return [
        {k: v for k, v in e.items() if k not in VOLATILE}
        for e in neutral(json.dumps(kinds_all(path)), root, sha)
        if not str(e["kind"]).startswith("node_")
    ]


COMPOSERS_OWN = ("workflow", "nodes_done")
"""What only the composer saves (the driver runs no workflow): the card's snapshot (D21) and the done set of
the nodes that ran (D17). The conditions' bools and the briefs' computed inputs are transient, never saved: a
story's `artifacts` stay empty, as the driver's do."""


def plan_state(path: Path, root: Path, sha: str, *, composer: bool) -> dict[str, Any]:
    """The saved state but its runs and what only the composer saves (`COMPOSERS_OWN`). The driver's raw
    state.json has none of those at all (`composer` says which saved it): the mask is for the comparison
    only, never a cover for the driver saving them."""
    saved = neutral((path / "state.json").read_text(), root, sha)
    if not composer:
        assert not set(COMPOSERS_OWN) & set(saved), f"the driver saved {set(COMPOSERS_OWN) & set(saved)}"
    return {k: v for k, v in saved.items() if k not in ("runs", *COMPOSERS_OWN)}


@pytest.mark.parametrize("survey", [False, True])
async def test_the_composer_plans_with_the_drivers_events_fields_and_state(
    tmp_path: Path, survey: bool
) -> None:
    """Driver and composer on the same story, each in its own directory: the same events with the same
    fields in the same order (but for times and run ids), and the same state.json. With the survey on, the
    survey runs beside refine (the plan_story parallel node), as `_refine_with_survey` runs it."""
    settings = {"survey": survey}
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        architect = [SURVEY_NOTES, LAYERS, *PKGS] if survey else [LAYERS, *PKGS]
        pipe = pipeline(root, repo, happy_engine(architect=architect), story=settings, composer=composer)
        st = await pipe.plan(text_card("add a marker"))
        assert st.blocked is None and st.stage == "layers"
        assert st.base is not None
        sha = st.base["sha"]
        seen.append(
            (
                plan_events(st.path / "events.jsonl", root, sha),
                plan_state(st.path, root, sha, composer=composer),
            )
        )
    (driver_events, driver_state), (composer_events, composer_state) = seen
    assert composer_events == driver_events
    assert composer_state == driver_state
    if survey:
        assert composer_state["survey"] == SURVEY_NOTES.text


async def test_a_plan_resumed_through_the_composer_matches_the_drivers_resume(tmp_path: Path) -> None:
    """A story stopped in the layer split (its base and gate stored) and planned again: the composer skips
    the nodes whose outputs are stored, and still reads the stored gate into the pipeline as `_gate` does."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = gated(root)
        nonsense = FakeReply(text="no plan here")
        first = pipeline(root, repo, happy_engine(architect=[nonsense] * 4), test_cmd=None, composer=composer)
        stopped = await first.plan(text_card("add a marker"))
        assert stopped.blocked and stopped.gate is not None and stopped.layers is None
        again = pipeline(root, repo, happy_engine(), test_cmd=None, composer=composer)
        st = await again.plan(text_card("add a marker"))
        assert st.blocked is None and st.stage == "layers"
        assert again.commands == ("./check.sh",)
        assert st.base is not None
        sha = st.base["sha"]
        seen.append(
            (
                plan_events(st.path / "events.jsonl", root, sha),
                plan_state(st.path, root, sha, composer=composer),
            )
        )
    assert seen[1] == seen[0]


def test_every_check_the_workflows_name_is_in_the_catalog_and_every_prompt_file_resolves(
    tmp_path: Path,
) -> None:
    """The shipped story and layer workflows name code only as catalog checks: each check or gate node's
    `run` is a declared check the story's registry resolves, each `prompt:<id>` is a hub rule, and the
    runners cover every node type the files use."""
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=True)
    card = text_card("add a marker")
    st = StoryState(path=tmp_path, card={}, repo=str(repo))
    registry = story_registry(pipe, st, card, until="review")
    nodes = [node for spec in pipe.cfg.workflows.values() for node in spec.nodes.values()]
    runs = {node.run for node in nodes if node.type in ("check", "gate")}
    assert None not in runs
    assert {"story.refine", "story.plan_layers", "story.draft_packages", "story.request"} <= runs
    assert {"story.ready_package", "story.place_layer", "story.squash_layer", "story.to_build"} <= runs
    assert "story.build_once" not in runs  # declared still, for a card on an old snapshot (D21)
    assert all(registry.check(str(run)) is STORY_CATALOG[str(run)].fn for run in sorted(map(str, runs)))
    prompts = {node.prompt for node in nodes if node.prompt is not None}
    assert prompts == {"prompt:story-survey", "prompt:amigos-opening"}
    assert {p.removeprefix("prompt:") for p in prompts} <= rule_ids()
    assert all(node.run is None for node in nodes if node.type in ("ask", "meeting"))
    runners = story_runners(pipe, st, card)
    assert set(runners) == {"ask", "conversation", "write", "meeting", "check", "gate", "team"}
    assert {node.type for node in nodes} - {"bundle"} <= set(runners)  # a bundle is the composer's own


def test_the_registry_holds_the_catalog_and_nothing_else(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=True)
    st = StoryState(path=tmp_path, card={}, repo=str(repo))
    registry = story_registry(pipe, st, text_card("add a marker"), until="review")
    assert registry.catalog is STORY_CATALOG
    assert all(registry.check(c.name) is c.fn for c in STORY_CATALOG)
    with pytest.raises(KeyError, match="story.nope"):
        registry.check("story.nope")


async def test_check_callables_read_inputs_from_the_store_not_the_pipeline(tmp_path: Path) -> None:
    """The gate check through the generic check runner reads its base from the store it is handed (the
    story's state has none): the gate read from that commit's AGENTS.md is its output, and `gate` is said."""
    repo = gated(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), test_cmd=None, composer=True)
    card = text_card("add a marker")
    st = StoryState(path=tmp_path / "story", card={}, repo=str(repo))
    st.path.mkdir()
    sha = git(repo, "rev-parse", "HEAD")
    base = {"ref": f"origin/{pipe.s.trunk}", "sha": sha}
    store = MemoryStore({"base": base})
    registry = story_registry(pipe, st, card, until="review")
    node = pipe.cfg.workflows["story"].nodes["gate"]
    out = await check_runner(node, run_context(pipe, registry, store, "gate"))
    assert st.base is None  # read from the store, never the pipeline's state
    assert out == {"gate": st.gate} and out["gate"]["commands"] == ["./check.sh"]
    gates = [e for e in kinds_all(st.path / "events.jsonl") if e["kind"] == "gate"]
    assert [e["base"] for e in gates] == [base]
    assert pipe.commands == ("./check.sh",)  # read into the pipeline, as the driver's gate step does


async def test_the_ask_runner_retries_a_fielded_output_until_it_parses(tmp_path: Path) -> None:
    """An ask whose output has fields: the rendered prompt is the brief, a reply the fields do not parse goes
    back with the reason in `{feedback}` (`Asker.usable`, one `retry` event at the node's stage), and the
    parsed fields are the output; nothing is appended to the prompt."""
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(product=[FakeReply(text="no story here"), STORY])
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    node = v2(
        type="ask",
        role="product",
        prompt="Refine: {card}.{?feedback} Last time: {feedback}{/feedback}",
        inputs=["card"],
        outputs={"story": {"format": "json", "fields": {"title": "text", "summary": "text"}}},
        steps=3,
    )
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "add a marker"}), "refine")
    out = await ask_runner(node, ctx, asker=pipe.asker, st=st)
    assert set(out) == {"story"} and out["story"]["title"] == "Add avatar upload"
    retries = [e for e in kinds_all(st.path / "events.jsonl") if e["kind"] == "retry"]
    assert [e["stage"] for e in retries] == ["refine"]
    first, second = briefs_to(engine, "product")
    assert "Refine: add a marker." in first and "Last time" not in first
    assert f"Refine: add a marker. Last time: {retries[0]['error']}" in second
    assert retries[0]["error"].startswith("the reply has no JSON object")  # parse_output's reason
    assert "Reply with" not in first  # outputs.describe is never appended to a prompt


async def test_an_ask_without_fields_asks_once_and_its_events_name_the_legacy_kinds(tmp_path: Path) -> None:
    """A node's `events`: on success its `done` kind with the node and the reply's length, the whole reply
    stored under its first output; on an `on_fail: continue` failure its `failed` kind with the reason, and
    the node fails without outputs; with `on_fail: block` the failure propagates."""
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(architect=[SURVEY_NOTES])
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    node = v2(
        type="ask",
        role="architect",
        prompt="Survey: {card}",
        inputs=["card"],
        outputs={"survey": "markdown"},
        steps=8,
        on_fail="continue",
        events={"done": "survey_done", "failed": "survey_failed"},
    )
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "add a marker"}), "survey")
    assert await ask_runner(node, ctx, asker=pipe.asker, st=st) == {"survey": SURVEY_NOTES.text}
    failed = await ask_runner(node, ctx, asker=pipe.asker, st=st)  # the architect's script is spent
    assert set(failed) == {FAIL_KEY}
    legacy = ("survey_done", "survey_failed")
    done, fail = [e for e in kinds_all(st.path / "events.jsonl") if e["kind"] in legacy]
    bare = [{k: v for k, v in e.items() if k not in ("ts", "run_id")} for e in (done, fail)]
    assert bare == [  # the driver's fields exactly: no node, the reason capped as the driver's
        {"kind": "survey_done", "chars": len(SURVEY_NOTES.text)},
        {"kind": "survey_failed", "reason": "StoryError: the architect did not finish (engine_error)"},
    ]
    assert "Survey: add a marker" in briefs_to(engine, "architect")[0]

    async def long_failure(*args: Any, **kwargs: Any) -> str:
        raise StoryError("x" * 3000)

    await ask_runner(node, ctx, asker=pipe.asker, st=st, ask=long_failure)
    capped = [e for e in kinds_all(st.path / "events.jsonl") if e["kind"] == "survey_failed"][-1]["reason"]
    assert capped == _cap(_reason(StoryError("x" * 3000))) and len(capped) == REASON_CHARS
    with pytest.raises(StoryError, match="the architect did not finish"):  # on_fail block: it propagates
        await ask_runner(node.model_copy(update={"on_fail": "block"}), ctx, asker=pipe.asker, st=st)
    assert [e["kind"] for e in kinds_all(st.path / "events.jsonl")].count("survey_failed") == 2


async def test_a_story_card_skips_refine_and_logs_refine_skipped(tmp_path: Path) -> None:
    """An epic's story card is taken as the story by the `take_card_as_story` check; refine (and the survey
    beside it) are skipped, and product is never asked."""
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(product=[])
    card = card_of_epic("AIR-4", planned("S1", "Add avatar upload"))
    st = await pipeline(tmp_path, repo, engine, story={"survey": True}, composer=True).plan(card)
    events = [(e["kind"], e) for e in kinds_all(st.path / "events.jsonl")]
    kinds = [k for k, _ in events]
    skipped = [f["node"] for k, f in events if k == "node_skipped"]
    assert "refine_skipped" in kinds and "refine" in skipped and "survey" in skipped
    assert not [r for r in engine.requests if r.role == "product"]
    assert st.blocked is None and st.story is not None and st.story["title"] == "Add avatar upload"


NOT_ACTIONABLE = FakeReply(text=fenced({"type": "blocked", "reason": "which image sizes?"}))


async def test_a_resumed_survey_quotes_the_card_as_it_is_now(tmp_path: Path) -> None:
    """The survey's request is transient: a story stopped before its survey ran (product blocked it beside
    the survey) and started again on an edited card is surveyed from the card as it is now."""
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("add a marker")
    first = happy_engine(product=[NOT_ACTIONABLE], architect=[SURVEY_NOTES])
    stopped = await pipeline(tmp_path, repo, first, story={"survey": True}, composer=True).plan(card)
    assert stopped.blocked is not None and stopped.survey is None and stopped.story is None
    edited = replace(card, description="add a marker, in red")
    again = happy_engine(architect=[SURVEY_NOTES, LAYERS])
    st = await pipeline(tmp_path, repo, again, story={"survey": True}, composer=True).plan(edited)
    assert st.blocked is None and st.survey == SURVEY_NOTES.text
    assert "> add a marker, in red" in briefs_to(again, "architect")[0]
    assert "artifacts" not in json.loads((st.path / "state.json").read_text())  # nothing transient saved


async def test_a_resumed_story_card_keeps_its_stored_story_whatever_its_block_says_now(
    tmp_path: Path,
) -> None:
    """A story card stopped after its story was taken, its block then broken: the stored story is the
    story, the block is not read again (`story.is_story_card` is false once a story is stored), and the
    story is planned, as the driver plans it."""
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        card = card_of_epic("AIR-4", planned("S1", "Add avatar upload"))
        nonsense = FakeReply(text="no plan here")
        first = happy_engine(product=[], architect=[nonsense] * 4)
        stopped = await pipeline(root, repo, first, composer=composer).plan(card)
        assert stopped.blocked is not None and stopped.story is not None and stopped.layers is None
        broken = replace(card, description=card.description.replace('"size": "S"', '"size": "huge"'))
        with pytest.raises(StoryBlocked):
            StoryPipeline._story_card(broken)  # the block as it is now cannot be read
        st = await pipeline(root, repo, happy_engine(product=[]), composer=composer).plan(broken)
        assert st.blocked is None and st.layers is not None, st.blocked
        assert st.story is not None and st.story["title"] == "Add avatar upload"


@pytest.mark.parametrize("case", ["retry", "story card", "not actionable", "survey fails"])
async def test_the_ask_nodes_edge_cases_run_the_same_either_way(tmp_path: Path, case: str) -> None:
    """The ask runner's other paths, driver beside composer: a reply that goes back once, a story card,
    product's refusal (the story blocks with today's reason) and a survey that fails (no notes, the story
    goes on): the same events with the same fields in the same order, and the same state.json."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        card = text_card("add a marker")
        settings: dict[str, object] = {"survey": True}
        if case == "retry":
            garbage = FakeReply(text="no story here")
            engine = happy_engine(product=[garbage, STORY], architect=[SURVEY_NOTES, LAYERS])
        elif case == "story card":
            engine = happy_engine(product=[])
            card = card_of_epic("AIR-4", planned("S1", "Add avatar upload"))
        elif case == "not actionable":
            engine = happy_engine(product=[NOT_ACTIONABLE], architect=[SURVEY_NOTES])
        else:
            engine = happy_engine(architect=[CUT_THINKING, LAYERS])
        st = await pipeline(root, repo, engine, story=settings, composer=composer).plan(card)
        assert st.base is not None
        sha = st.base["sha"]
        seen.append(
            (
                plan_events(st.path / "events.jsonl", root, sha),
                plan_state(st.path, root, sha, composer=composer),
            )
        )
    assert seen[1][0] == seen[0][0]
    assert seen[1][1] == seen[0][1]
    events, state = seen[0]
    if case == "not actionable":
        assert "product says this cannot be built as written: which image sizes?" in str(state["blocked"])
    if case == "survey fails":
        assert state["survey"] == "" and any(e["kind"] == "survey_failed" for e in events)
    if case == "retry":
        assert [e["kind"] for e in events].count("retry") == 1


# ---- the build lane through the composer (story.composer), against the driver ----

# captured once from the driver (composer=False): the contract the composer meets
EXPECTED_RUN_KINDS = [
    "story_start", "stage", "run", "stage", "run", "stage",
    "packages_drafting", "run", "package_draft_start", "package_drafted", "package_draft_start",
    "package_drafted",
    "test_checks_skipped", "package_start", "package_checks_start", "package_ready", "layer_start", "brief",
    "package_placed", "layer_built",
    "test_checks_skipped", "package_start", "package_checks_start", "package_ready", "layer_start", "brief",
    "package_placed", "layer_built",
    "stage", "published", "stage", "stage", "story_done",
]  # fmt: skip


def built_run(st: StoryState, root: Path, *, composer: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """`plan_events` and `plan_state` of a story that built layers: each commit (its own in every
    repository, as the commit times differ) named by the order it first appears in, as are its short names,
    and every studio run id and pytest's time in a gate's output ("1 failed in 0.01s") spelled the same.
    `composer` says which ran it, and the raw log must agree: node events only under the composer."""
    assert st.base is not None
    started = [e for e in kinds_all(st.path / "events.jsonl") if e["kind"] == "node_started"]
    assert bool(started) is composer, f"composer={composer} but {len(started)} node_started events"

    def same(text: str) -> str:
        return re.sub(r" in \d+\.\d+s\b", " in <s>", re.sub(r"\b\d{8}-\d{6}-[0-9a-f]{4}\b", "<run>", text))

    events = same(json.dumps(plan_events(st.path / "events.jsonl", root, st.base["sha"])))
    state = same(json.dumps(plan_state(st.path, root, st.base["sha"], composer=composer)))
    shas = list(dict.fromkeys(re.findall(r"\b[0-9a-f]{40}\b", events + state)))
    for n, sha in enumerate(shas):
        events, state = (re.sub(rf"\b{sha[:7]}[0-9a-f]*\b", f"<commit {n}>", t) for t in (events, state))
    return json.loads(events), json.loads(state)


@pytest.mark.parametrize("composer", [False, True])
async def test_a_story_builds_publishes_and_reaches_review_the_same_either_way(
    tmp_path: Path, composer: bool
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=composer)
    st = await pipe.run(text_card("add a marker"))
    assert st.blocked is None and st.stage == "review", st.blocked
    assert plan_kinds(st.path / "events.jsonl") == EXPECTED_RUN_KINDS
    assert list(st.built) == ["01", "02"] and [p["number"] for p in st.prs] == [11, 12]
    stages = [e["stage"] for e in kinds_all(st.path / "events.jsonl") if e["kind"] == "stage"]
    assert stages == ["intake", "refine", "layers", "build", "publish", "review"]


async def test_the_composer_runs_a_story_with_the_drivers_events_fields_and_state(tmp_path: Path) -> None:
    """A full `run` through the composer, beside the driver: the same events with the same fields in the same
    order (but for times and run ids) and the same state.json, from intake to review."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        st = await pipeline(root, repo, happy_engine(), composer=composer).run(text_card("add a marker"))
        assert st.blocked is None and st.stage == "review", st.blocked
        # the composer ran the story's snapshot, taken when the card started (D21); the driver takes none
        assert (st.workflow or {}).get("name") == ("story" if composer else None)
        seen.append(built_run(st, root, composer=composer))
    (driver_events, driver_state), (composer_events, composer_state) = seen
    assert composer_events == driver_events
    assert composer_state == driver_state


@pytest.mark.parametrize("ends", ["built", "blocked"])
async def test_a_layer_sent_back_to_the_architect_runs_the_same_through_the_composers_loop(
    tmp_path: Path, ends: str
) -> None:
    """story.layer_attempts 2: a layer red after the coder's follow-up goes back to the architect with the
    feedback (the build node's loop to package, carrying it) and builds again; red again, the story stops on
    the last build, as `_build_once` stops it. The same events and state as the driver's."""
    second = [FEATURE, DONE] if ends == "built" else [BROKEN, DONE, STILL_BROKEN, DONE]
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        engine = FakeEngine(
            {
                "product": [STORY],
                "architect": [CALC_LAYERS, RED, OTHER_RED],
                "coder": [BROKEN, DONE, STILL_BROKEN, DONE, *second],
                "reviewer": [APPROVE, APPROVE],
            }
        )
        repo = calc_repo(root)
        cmd = f"{shlex.quote(sys.executable)} -m pytest -q"
        pipe = pipeline(root, repo, engine, test_cmd=cmd, story={"layer_attempts": 2}, composer=composer)
        st = await pipe.run(text_card("multiply"))
        assert (st.blocked is None) == (ends == "built"), st.blocked
        events, state = built_run(st, root, composer=composer)
        assert [e["attempt"] for e in events if e["kind"] == "layer_start"] == [1, 2]
        seen.append((events, state))
    assert seen[1] == seen[0]


async def test_a_resumed_build_skips_the_layers_already_built(tmp_path: Path) -> None:
    """A story built and published, then put back to its planned stage with layer 02's build and the PRs
    forgotten, and built again. Through the composer, layer 01's steps are skipped: the line from `start`
    to `covered` dies for a built layer, and every step below it dies with it. `start` and `costed_layer`
    run on every run, as the driver costs every layer, though the first run's done set is still saved.
    Layer 02 is built again from a fresh package, with the driver's events and state."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        card = text_card("add a marker")
        first = await pipeline(root, repo, happy_engine(), composer=composer).run(card)
        assert first.blocked is None and first.stage == "review"
        saved = json.loads((first.path / "state.json").read_text())
        del saved["built"]["02"]
        saved["prs"], saved["stage"] = [], "layers"
        (first.path / "state.json").write_text(json.dumps(saved))
        git(repo, "branch", "-D", B2)  # gone with its record, as a layer stopped part-way leaves none
        log = first.path / "events.jsonl"
        before = len(log.read_text().splitlines())
        engine = happy_engine(architect=[PKGS[1]], coder=[NOTE, DONE], reviewer=[APPROVE])
        st = await pipeline(root, repo, engine, composer=composer).build(card)
        assert st.blocked is None and st.stage == "review", st.blocked
        assert list(st.built) == ["01", "02"] and [p["number"] for p in st.prs] == [11, 12]
        events = kinds_all(log)[before:]
        assert [e["layer"] for e in events if e["kind"] == "layer_start"] == ["02"]
        if composer:
            skipped = [(e["node"], e["item"]) for e in events if e["kind"] == "node_skipped"]
            assert [node for node, item in skipped if item == "01"] == [
                "covered", "drafts", "package", "place", "build", "done_check", "review", "fix", "final_gate",
                "gate_advice", "follow_up", "regate", "outcome", "squash",
            ]  # fmt: skip
            assert ("drafts", "02") in skipped  # the drafting was held once, before layer 01
        seen.append(built_run(st, root, composer=composer))
    assert seen[1] == seen[0]


async def test_a_story_put_back_to_publish_publishes_again_without_a_stale_gate_or_bool(
    tmp_path: Path,
) -> None:
    """A story built and published, put back to before its publish (its PRs forgotten) and built again: no
    condition's bool was saved, so `to_build` is computed afresh (every layer is built: false) and the gate
    is not read again, the publish runs, with the driver's events and state."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        card = text_card("add a marker")
        first = await pipeline(root, repo, happy_engine(), composer=composer).run(card)
        assert first.blocked is None and first.stage == "review"
        saved = json.loads((first.path / "state.json").read_text())
        assert "artifacts" not in saved  # the bools and computed inputs are transient
        saved["prs"], saved["stage"] = [], "build"
        (first.path / "state.json").write_text(json.dumps(saved))
        log = first.path / "events.jsonl"
        before = len(log.read_text().splitlines())
        st = await pipeline(root, repo, FakeEngine({}), composer=composer).build(card)
        assert st.blocked is None and st.stage == "review", st.blocked
        later = kinds_all(log)[before:]
        assert "gate" not in [e["kind"] for e in later] and "published" in [e["kind"] for e in later]
        if composer:
            ran = {e["node"] for e in later if e["kind"] == "node_started"}
            assert "to_build" in ran and "gate" not in ran
        seen.append(built_run(st, root, composer=composer))
    assert seen[1] == seen[0]


@pytest.mark.parametrize("how", ["reply", "below"])
async def test_a_satisfied_layer_runs_the_same_through_the_composer(tmp_path: Path, how: str) -> None:
    """Layer 02 needs no code: the architect's package reply says so (`package` satisfies it, and `build`
    is skipped), or layer 01's package already covers its criterion (`covered` satisfies it). The same
    events and state as the driver's."""
    both = package_reply(
        {"tests/unit/test_storage.py": "def test_stores_an_avatar():\n    assert True\n"},
        {
            "AC1": ["tests/unit/test_storage.py::test_stores_an_avatar"],
            "AC2": ["tests/unit/test_storage.py::test_stores_an_avatar"],
        },
    )
    architect = [LAYERS, PKG1, SATISFIED] if how == "reply" else [LAYERS, both, PKG2]
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        engine = happy_engine(architect=architect, coder=[WRITE_PASS, RUN, DONE], reviewer=[APPROVE])
        st = await pipeline(root, repo, engine, composer=composer).run(text_card("add a marker"))
        assert st.blocked is None and st.stage == "review", st.blocked
        assert list(st.built) == ["01"] and list(st.satisfied) == ["02"]
        seen.append(built_run(st, root, composer=composer))
    assert seen[1] == seen[0]


# ---- the dissolved layer bundle (generic nodes Part 2, Task 5): the build's steps as check nodes over one
# layer studio, beside the driver's `_build_once` on the same script. Only the composer's node events may
# differ: the story's events, every studio run's events and prompts, the ledger rows and state.json match.


async def layer_both_ways(
    tmp_path: Path,
    engine: Callable[[], FakeEngine],
    *,
    story: dict[str, object] | None = None,
    studio: type[StudioOrchestrator] = StudioOrchestrator,
    supervisor: bool = True,
    done_check: bool = False,
    test_cmd: str = PYTEST,
) -> list[tuple[StoryState, Path]]:
    """The one-layer calc story run by the driver, then by the composer, each in its own root on a fresh
    `engine()`: each run's state and root."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        settings: dict[str, object] = {"supervisor": {"enabled": True}} if supervisor else {}
        pipe = pipeline(
            root,
            calc_repo(root),
            engine(),
            test_cmd=test_cmd,
            story={**settings, **(story or {})},
            roles=SUPERVISOR_ROLES,
            done_check=done_check,
            composer=composer,
            studio=studio,
        )
        seen.append((await pipe.run(text_card("multiply")), root))
    return seen


def ledger_rows(root: Path) -> list[tuple[Any, ...]]:
    return [
        (r.trigger, r.subtask, r.role, r.kinds, r.category, r.owner, r.message, r.outcome, r.layer)
        for r in rows(root)
    ]


Events = list[dict[str, Any]]


def story_trace(st: StoryState, root: Path) -> tuple[Events, list[Events], list[str]]:
    """The story's events, each studio run's events (in the order the story named its runs) and every
    prompt the runs sent, with `root` spelled `<root>`."""

    def rooted(path: Path) -> str:
        return path.read_text().replace(str(root.resolve()), "<root>").replace(str(root), "<root>")

    runs = [root / "state" / "runs" / r for r in st.runs]
    assert {p.name for p in runs} == {p.name for p in (root / "state" / "runs").iterdir() if p.is_dir()}
    events = untraced([json.loads(x) for x in rooted(st.path / "events.jsonl").splitlines()])
    studio = [[json.loads(x) for x in rooted(r / "events.jsonl").splitlines()] for r in runs]
    prompts = []
    for r in runs:
        for f in sorted((r / "trace").rglob("prompt.json")):
            saved = json.loads(rooted(f))
            said = "\n".join(f"[{m['role']}] {m['content']}" for m in saved["messages"])
            prompts.append(f"[role={saved.get('role')} attempt={saved.get('attempt')}]\n{said}")
    return events, studio, prompts


def same_either_way(runs: list[tuple[StoryState, Path]]) -> None:
    """The story's events, every studio run's events and prompts (the trace diff's comparisons, which drop
    the node events and what differs run to run), every ledger row's fields, and the story's events and
    state.json as `built_run` reads them: the same for the driver and the composer."""
    (driver, d_root), (composer, c_root) = runs
    (d_story, d_runs, d_prompts), (c_story, c_runs, c_prompts) = (
        story_trace(driver, d_root),
        story_trace(composer, c_root),
    )
    assert len(c_runs) == len(d_runs)
    found = diff_events(d_story, c_story) + diff_prompts(d_prompts, c_prompts, cap=1000)
    for n, (d, c) in enumerate(zip(d_runs, c_runs, strict=True)):
        found += [f"run {n}: {line}" for line in diff_events(d, c)]
    assert not found, "\n".join(found)
    assert ledger_rows(c_root) == ledger_rows(d_root)
    (d_events, d_state), (c_events, c_state) = (
        built_run(driver, d_root, composer=False),
        built_run(composer, c_root, composer=True),
    )
    assert untraced(c_events) == untraced(d_events)
    assert c_state == d_state


def untraced(events: Events) -> Events:
    """An `error` event's traceback names the frames it went through: the driver's or the composer's."""
    return [{**e, "traceback": "<traceback>"} if "traceback" in e else e for e in events]


def nodes_run(st: StoryState, kind: str = "node_started") -> list[str]:
    """The layer bundle's nodes for layer 01, in the order the composer started (or skipped) them."""
    return [
        e["node"] for e in kinds_all(st.path / "events.jsonl") if e["kind"] == kind and e.get("item") == "01"
    ]


def studio_runs(st: StoryState, root: Path) -> list[Events]:
    """The events of each studio run of the story, in the order it named them (run ids that start in the
    same second sort by chance)."""
    return [kinds_all(root / "state" / "runs" / r / "events.jsonl") for r in st.runs]


def run_kinds(st: StoryState, root: Path) -> list[list[str]]:
    return [[e["kind"] for e in run] for run in studio_runs(st, root)]


def engine_of(**scripts: list[Any]) -> Callable[[], FakeEngine]:
    """A fresh engine on the calc story's replies (`scripted`'s), any role's replaced by `scripts`."""
    base = {"product": [STORY], "architect": [CALC_LAYERS, RED], "reviewer": [APPROVE, APPROVE]}
    return lambda: FakeEngine({role: list(replies) for role, replies in {**base, **scripts}.items()})


async def test_the_dissolved_layer_runs_the_golden_script_the_same_either_way(tmp_path: Path) -> None:
    """Task 1's script (a done-check miss, a blocking review and its fix, a red gate with the supervisor's
    advice, the follow-up and a green regate) through the layer bundle's nodes: the golden story, studio,
    ledger, state and prompts, as the driver gives them."""
    runs = await layer_both_ways(tmp_path, layer_engine, done_check=True)
    same_either_way(runs)
    st, root = runs[1]
    assert st.blocked is None and list(st.built) == ["01"], st.blocked
    story = [e for e in clean(kinds_all(st.path / "events.jsonl"), root) if not e["kind"].startswith("node_")]
    assert [e["kind"] for e in story] == GOLDEN_STORY_KINDS
    layer = [
        e for e in story if e["kind"] in {"layer_start", "package_placed", "supervisor_triage", "layer_built"}
    ]
    assert layer == GOLDEN_STORY_LAYER
    assert clean(kinds_all(build_run(root) / "events.jsonl"), root, drop=TIMING) == GOLDEN_STUDIO
    assert ledger_rows(root) == GOLDEN_LEDGER
    assert golden_state(root) == GOLDEN_STATE
    assert golden_prompts(root) == GOLDEN_NUDGES
    # each step its own node, the fix's line going back to the review once
    assert nodes_run(st) == [
        "start", "covered", "drafts", "package", "place", "build", "done_check", "review", "fix", "review",
        "final_gate", "gate_advice", "follow_up", "regate", "outcome", "squash", "costed_layer",
    ]  # fmt: skip


async def test_review_rounds_running_out_still_reach_the_gate_either_way(tmp_path: Path) -> None:
    """studio.review.rounds 1 and two blocking reviews: the review after the one fix round still runs and
    goes to the gate, which builds the layer; the review bound never blocks it (Review Focus 2)."""
    engine = engine_of(
        coder=[FEATURE, DONE, write("README.md", "mul multiplies.\n"), DONE], reviewer=[CHANGES, CHANGES]
    )
    runs = await layer_both_ways(tmp_path, engine, supervisor=False)
    same_either_way(runs)
    st, root = runs[1]
    assert st.blocked is None and list(st.built) == ["01"], st.blocked
    kinds_of = run_kinds(st, root)[-1]
    assert kinds_of.count("review") == 2 and kinds_of.count("fix_round") == 1 and "gate_passed" in kinds_of
    assert nodes_run(st).count("review") == 2 and "final_gate" in nodes_run(st)


async def test_a_layer_sent_back_after_a_fix_round_gets_its_review_rounds_again_either_way(
    tmp_path: Path,
) -> None:
    """story.layer_attempts 2, a blocking review and its fix on each build: the second build has review
    rounds of its own (the fix's loop back to the review is counted per build, as the driver's are)."""
    readme, again = write("README.md", "mul multiplies.\n"), write("notes.txt", "again\n")
    engine = engine_of(
        architect=[CALC_LAYERS, RED, OTHER_RED],
        coder=[BROKEN, DONE, STILL_BROKEN, DONE, again, DONE, FEATURE, DONE, readme, DONE],
        reviewer=[CHANGES, APPROVE, CHANGES, APPROVE],
    )
    runs = await layer_both_ways(tmp_path, engine, story={"layer_attempts": 2}, supervisor=False)
    same_either_way(runs)
    st, root = runs[1]
    assert st.blocked is None and list(st.built) == ["01"], st.blocked
    assert [ks.count("fix_round") for ks in run_kinds(st, root)[-2:]] == [1, 1]
    assert [ks.count("review") for ks in run_kinds(st, root)[-2:]] == [2, 2]


async def test_a_wrong_test_verdict_at_the_gate_skips_the_follow_up_either_way(tmp_path: Path) -> None:
    """The supervisor says the acceptance test is wrong: no follow-up turn, and the package retry carries
    `package-test-wrong` before `package-replan` (Review Focus 3)."""
    wrong = answer("MMMDCCCLXXXIX is 3889: correct the test.", category="wrong_acceptance_test")
    engine = engine_of(
        architect=[CALC_LAYERS, RED, RED], coder=[BROKEN, DONE, FIXED, DONE], supervisor=[wrong]
    )
    runs = await layer_both_ways(tmp_path, engine, story={"layer_attempts": 2})
    same_either_way(runs)
    st, root = runs[1]
    assert st.blocked is None and list(st.built) == ["01"], st.blocked
    first = run_kinds(st, root)[-2]
    assert "gate_failed" in first and "follow_up" not in first
    said = [e["id"] for e in kinds(st.path / "events.jsonl", "steer") if e["role"] == "architect"]
    assert said[-2:] == ["package-test-wrong", "package-replan"], said
    assert "gate_advice" in nodes_run(st) and "follow_up" not in nodes_run(st)


async def test_a_cut_off_coder_gets_no_gate_follow_up_either_way(tmp_path: Path) -> None:
    """The coder is cut off with the gate red: no gate_failed, no advice asked, no follow-up; the layer goes
    back to the architect and builds (Review Focus 6)."""
    coder = [BROKEN, CUT_THINKING, FEATURE, DONE]
    engine = engine_of(architect=[CALC_LAYERS, RED, RED], coder=coder, supervisor=[])
    runs = await layer_both_ways(tmp_path, engine, story={"layer_attempts": 2})
    same_either_way(runs)
    st, root = runs[1]
    assert st.blocked is None and list(st.built) == ["01"], st.blocked
    first = studio_runs(st, root)[-2]
    assert {"gate_failed", "follow_up"}.isdisjoint(e["kind"] for e in first)
    assert [e["stop_reason"] for e in first if e["kind"] == "subtask_done"] == ["cut_off"]
    assert rows(root) == []  # the supervisor was asked nothing
    assert "gate_advice" in nodes_run(st, "node_skipped") and "gate_advice" not in nodes_run(st)


async def test_a_layer_that_changed_nothing_is_replanned_either_way(tmp_path: Path) -> None:
    """A gate that passes on a coder that wrote nothing: `changed nothing`, a replan and the next build
    (Review Focus 4)."""
    # the first build's coder says it is done twice (the first is nudged to act) and writes nothing
    engine = engine_of(architect=[CALC_LAYERS, RED, RED], coder=[DONE, DONE, FEATURE, DONE], supervisor=[])
    runs = await layer_both_ways(tmp_path, engine, story={"layer_attempts": 2}, test_cmd="true")
    same_either_way(runs)
    st, _ = runs[1]
    assert st.blocked is None and list(st.built) == ["01"], st.blocked
    [replanned] = kinds(st.path / "events.jsonl", "layer_replanned")
    assert (replanned["why"], replanned["detail"]) == ("gate_red", "changed nothing")


class KilledAfterCoder(StudioOrchestrator):
    """A studio whose first coder turn finishes, and then the run is killed."""

    killed = False

    async def coder_turn(self, subtask: Subtask) -> SubtaskRun:
        run = await super().coder_turn(subtask)
        if not KilledAfterCoder.killed:
            KilledAfterCoder.killed = True
            raise asyncio.CancelledError
        return run


async def test_a_run_killed_after_the_coder_turn_resumes_from_place_either_way(tmp_path: Path) -> None:
    """Killed between the coder turn and the squash: the layer has no built record, its studio is gone and
    its attempt names a kept worktree. The next build places the package again on the same branch and runs
    every step after it, none skipped (Review Focus 1)."""
    seen = []
    for composer in (False, True):
        KilledAfterCoder.killed = False
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo, card = calc_repo(root), text_card("multiply")
        kw: dict[str, Any] = {"test_cmd": PYTEST, "roles": SUPERVISOR_ROLES, "composer": composer}
        first = pipeline(root, repo, engine_of(coder=[FEATURE, DONE])(), studio=KilledAfterCoder, **kw)
        with pytest.raises(asyncio.CancelledError):
            await first.run(card)
        saved = StoryState.open(first.stories_dir / card.key, card, repo)
        assert saved.built == {} and saved.attempt is not None and Path(saved.attempt["worktree"]).is_dir()
        log = saved.path / "events.jsonl"
        before = len(log.read_text().splitlines())
        engine = FakeEngine({"architect": [RED], "coder": [FEATURE, DONE], "reviewer": [APPROVE]})
        st = await pipeline(root, repo, engine, **kw).build(card)
        assert st.blocked is None and list(st.built) == ["01"], st.blocked
        starts = kinds(log, "layer_start")
        assert len(starts) == 2 and starts[0]["branch"] == starts[1]["branch"]
        if composer:
            later = kinds_all(log)[before:]
            ran = [e["node"] for e in later if e["kind"] == "node_started" and e["item"] == "01"]
            skipped = {e["node"] for e in later if e["kind"] == "node_skipped" and e["item"] == "01"}
            after = ran[ran.index("place") :]
            assert after == ["place", "build", "done_check", "review", "final_gate", "outcome", "squash",
                             "costed_layer"]  # fmt: skip
            assert skipped.isdisjoint(after) and "covered" in ran
        seen.append((st, root))
    same_either_way(seen)


async def test_the_last_attempt_blocks_either_way(tmp_path: Path) -> None:
    """Red after the follow-up on the last build: StoryBlocked with the gate's reason, the worktree kept for
    the restart and the supervisor's rows closed (the block's diagnosis, the retry not helped)."""
    engine = engine_of(
        coder=[BROKEN, DONE, STILL_BROKEN, DONE],
        supervisor=[answer("Put add back."), answer(None, fix="restore add to return a + b")],
    )
    runs = await layer_both_ways(tmp_path, engine)
    same_either_way(runs)
    st, root = runs[1]
    assert st.blocked is not None and "fails on" in st.blocked
    assert st.attempt is not None and Path(st.attempt["worktree"]).is_dir() and st.built == {}
    assert [r.outcome for r in rows(root)] == ["blocked", "not_helped"]
    assert read_phase(root / "state" / "stories")[0] != "decoding"


class AdvisedThenDies(StudioOrchestrator):
    """A coder turn that asks the supervisor (a ledger row opens), then raises past the turn's own catch."""

    async def coder_turn(self, subtask: Subtask) -> SubtaskRun:
        assert self.advisor is not None
        await self.advisor.advise("gate_retry", subtask.id, "coder", "the gate is red", kinds=("gate_fails",))
        raise RuntimeError("the engine fell over")


async def test_a_node_that_raises_mid_layer_closes_its_rows_unknown_and_keeps_the_worktree_either_way(
    tmp_path: Path,
) -> None:
    """The coder's node raises: the layer studio `place` opened is closed (its open ledger row `unknown`, the
    build lane out of its decode) and the worktree stays for the restart, as the driver's `finally` does
    (Review Focus 5)."""
    engine = engine_of(coder=[], supervisor=[answer("Put add back as it was.")])
    runs = await layer_both_ways(tmp_path, engine, studio=AdvisedThenDies)
    same_either_way(runs)
    st, root = runs[1]
    assert st.blocked == "RuntimeError: the engine fell over"
    assert [r.outcome for r in rows(root) if r.trigger == "gate_retry"] == ["unknown"]
    assert st.attempt is not None and Path(st.attempt["worktree"]).is_dir()
    assert read_phase(root / "state" / "stories")[0] != "decoding"


def old_layer_snapshot() -> dict[str, Any]:
    """The story and layer workflows as a card that started before the layer's build was split snapshotted
    them: the build one `story.build_once` node."""
    specs = {
        name: raw
        for part in ("story", "layer")
        for name, raw in yaml.safe_load((WORKFLOWS / f"{part}.yaml").read_text())["workflows"].items()
    }
    nodes = specs["layer"]["nodes"]
    for name in ("place", "build", "done_check", "review", "fix", "final_gate", "gate_advice", "follow_up",
                 "regate", "outcome", "squash"):  # fmt: skip
        del nodes[name]
    nodes["package"]["to"][0]["node"] = "build"
    nodes["build"] = {
        "type": "check",
        "run": "story.build_once",
        "inputs": ["card", "repo", "package.{item}", "story", "layers"],
        "outputs": {"built.{item}": "json"},
        "to": [
            {"node": "package", "loop": {"max": "story.layer_attempts", "carry": "feedback"}},
            "costed_layer",
        ],
    }
    return snapshot("story", {n: WorkflowSpec.model_validate(s) for n, s in specs.items()})


async def test_a_card_on_the_old_layer_snapshot_still_builds_through_build_once(tmp_path: Path) -> None:
    """A card in flight finishes on its snapshot (D21): its layer's one build node runs `story.build_once`,
    now the wrapper over the steps, with the driver's events and state."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo, card = calc_repo(root), text_card("multiply")
        kw: dict[str, Any] = {"test_cmd": PYTEST, "roles": SUPERVISOR_ROLES, "done_check": True}
        story = {"supervisor": {"enabled": True}}
        pipe = pipeline(root, repo, layer_engine(), story=story, composer=composer, **kw)
        if composer:
            started = StoryState.open(pipe.stories_dir / card.key, card, repo)
            started.workflow = old_layer_snapshot()
            started.save()
        st = await pipe.run(card)
        assert st.blocked is None and list(st.built) == ["01"], st.blocked
        if composer:
            assert "build" in nodes_run(st) and "place" not in nodes_run(st)
        seen.append((st, root))
    same_either_way(seen)


# ---- the conversation type (`conversation_runner`): held conversations, and sessions that cross nodes ----


class TalkingAsker:
    """An asker whose conversations reply from a script: each opening and close recorded, and every turn's
    nudge (None for the opening turn)."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.opened: list[tuple[str, str, str, int | None]] = []
        self.said: list[str | None] = []
        self.closed = 0

    @contextlib.asynccontextmanager
    async def conversation(
        self, st: Any, stage: str, role: str, brief: str, *, steps: int | None, **_: Any
    ) -> AsyncIterator[Any]:
        self.opened.append((stage, role, brief, steps))
        st.runs.append(f"run-{len(self.opened)}")

        async def say(nudge: str | None = None, /, *, at: str | None = None, steps: int | None = None) -> str:
            self.said.append(nudge)
            return self.replies.pop(0)

        try:
            yield say
        finally:
            self.closed += 1


def talk_composer(tmp_path: Path, nodes: dict[str, Any], runners: dict[str, Any]) -> Composer:
    """A composer over one schema v2 workflow `w` of `nodes`."""
    spec = WorkflowSpec.model_validate({"nodes": nodes})
    return Composer(
        cfg_for(tmp_path),
        runners=runners,
        registry=Registry(),
        store=MemoryStore(),
        sessions=Sessions(),
        workflows={"w": spec},
    )


PLAN_FIELDS = {"plan": {"format": "json", "fields": {"layers": "list of text"}}}


async def test_the_conversation_runner_holds_its_session_for_the_run_and_ends_on_the_parsed_turn(
    tmp_path: Path,
) -> None:
    """The generic turn loop: the rendered prompt opens the conversation (turn 0), each later turn sends the
    rendered `turn` text with the reason the last reply did not parse, and the conversation ends on the
    first reply the fielded output parses; the session is stored under the session output and the next node
    that names it continues it, its rendered prompt one more turn of the same conversation; the composer
    closes it when the run ends, and the session is dropped then."""
    nodes = {
        "talk": {
            "type": "conversation",
            "role": "architect",
            "prompt": "the opening",
            "turn": "again: {feedback}",
            "steps": 5,
            "outputs": {**PLAN_FIELDS, "talk_session": "session"},
            "to": ["more"],
        },
        "more": {
            "type": "conversation",
            "role": "architect",
            "prompt": "one more for {plan.layers}",
            "session": "talk_session",
            "inputs": ["talk_session", "plan"],
            "outputs": {"more": "text"},
        },
    }
    plan = '```json\n{"layers": ["a", "b"]}\n```'
    asker = TalkingAsker(["first", "second", plan, "after"])
    st = SimpleNamespace(runs=[])
    held: list[Any] = []

    async def converse(node: Any, ctx: RunContext) -> Any:
        out = await conversation_runner(node, ctx, asker=cast(Any, asker), st=cast(Any, st))
        held.append((out.get("talk_session") or ctx.sessions.get("talk_session"), asker.closed))
        return out

    c = talk_composer(tmp_path, nodes, {"conversation": converse})
    await c.run("w")
    assert asker.opened == [("talk", "architect", "the opening", None)]  # one conversation, opened once
    assert asker.said[0] is None and len(asker.said) == 4  # it ended on the third turn, when it parsed
    assert asker.said[1].startswith("again: ") and "JSON" in asker.said[1]  # the parse reason
    assert asker.said[3] == 'one more for [\n "a",\n "b"\n]'
    assert c.store.get("plan") == {"layers": ["a", "b"]} and c.store.get("more") == "after"
    [(session, closed), (again, still)] = held
    assert isinstance(session, Session) and session.run == "run-1" and again is session
    assert closed == still == 0  # held while the run goes on
    assert asker.closed == 1 and c.sessions.get("talk_session") is None  # closed, and dropped, at its end


async def test_a_conversation_that_never_parses_within_its_steps_blocks_or_continues(tmp_path: Path) -> None:
    asker = TalkingAsker(["no", "still no", "no", "still no"])
    st = SimpleNamespace(runs=[], emit=lambda kind, **f: None)
    node = v2(type="conversation", role="architect", prompt="go", turn="again", steps=2, outputs=PLAN_FIELDS)
    ctx = RunContext(
        cfg=cfg_for(tmp_path),
        store=MemoryStore(),
        sessions=Sessions(),
        registry=Registry(),
        workflow="w",
        node="talk",
        item=None,
        feedback=None,
    )
    with pytest.raises(StoryBlocked, match="architect did not produce a usable plan"):
        await conversation_runner(node, ctx, asker=cast(Any, asker), st=cast(Any, st))
    cont = node.model_copy(update={"on_fail": "continue"})
    assert set(await conversation_runner(cont, ctx, asker=cast(Any, asker), st=cast(Any, st))) == {FAIL_KEY}
    assert asker.said == [None, "again", None, "again"] and asker.closed == 2


async def test_the_conversation_runner_talks_through_the_asker_with_the_fake_engine(tmp_path: Path) -> None:
    """Through the real `Asker.conversation`: the opening is the studio subtask, the turn a follow-up in the
    same conversation, and the parsed reply the output."""
    repo = repo_with_marker_tests(tmp_path)
    plan = FakeReply(text='Here.\n```json\n{"layers": ["storage"]}\n```')
    engine = happy_engine(architect=[FakeReply(text="thinking aloud"), plan])
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    node = v2(
        type="conversation",
        role="architect",
        prompt="Plan {card}.",
        turn="Again, as one json block: {feedback}",
        inputs=["card"],
        steps=3,
        outputs=PLAN_FIELDS,
    )
    ctx = run_context(pipe, Registry(), MemoryStore({"card": "add a marker"}), "layers")
    out = await conversation_runner(node, ctx, asker=pipe.asker, st=st)
    assert out == {"plan": {"layers": ["storage"]}}
    first, second = briefs_to(engine, "architect")
    assert "Plan add a marker." in first and "Again, as one json block: " in second
    assert len(st.runs) == 1  # one studio run: the second turn was a follow-up in it


async def test_the_composer_closes_its_held_conversations_when_the_run_raises(tmp_path: Path) -> None:
    nodes = {
        "talk": {
            "type": "conversation",
            "role": "architect",
            "prompt": "the opening",
            "outputs": {"said": "text", "s": "session"},
            "to": ["boom"],
        },
        "boom": {"type": "check", "run": "boom"},
    }
    asker = TalkingAsker(["first"])
    st = SimpleNamespace(runs=[])

    async def boom(node: Any, ctx: RunContext) -> Any:
        assert asker.closed == 0
        raise RuntimeError("boom")

    async def converse(node: Any, ctx: RunContext) -> Any:
        return await conversation_runner(node, ctx, asker=cast(Any, asker), st=cast(Any, st))

    c = talk_composer(tmp_path, nodes, {"conversation": converse, "check": boom})
    assert c.held is None  # nothing can be held before the run, or after it
    with pytest.raises(RuntimeError, match="boom"):
        await c.run("w")
    assert asker.said == [None] and asker.closed == 1 and c.held is None
    ctx = RunContext(
        cfg=c.cfg,
        store=c.store,
        sessions=c.sessions,
        registry=c.registry,
        workflow="w",
        node="talk",
        item=None,
        feedback=None,
    )
    with pytest.raises(RuntimeError, match="holds nothing open"):  # a session node outside a run: loud
        await converse(c.spec("w").nodes["talk"], ctx)


# ---- the write type (`write_runner`): one writing turn, its diff, its session ----


async def test_a_write_node_yields_its_diff_its_account_and_a_session_a_later_node_continues(
    tmp_path: Path,
) -> None:
    """A write node is one writing turn of a conversation detached at the base: its outputs are the diff
    since that base (new files included), the reply and the held session; a later write node that names the
    session sends its rendered prompt as the next turn, and its diff is the whole change since the base."""
    repo = repo_with_marker_tests(tmp_path)
    more = FakeReply(
        tool_calls=[ToolCall("m", "write_file", json.dumps({"path": "MORE", "content": "more\n"}))]
    )
    engine = happy_engine(
        coder=[WRITE_PASS, DONE, more, FakeReply(text="added MORE")], reviewer=[APPROVE] * 4
    )
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    nodes = {
        "build": {
            "type": "write",
            "role": "coder",
            "prompt": "Write PASS for {card}.",
            "inputs": ["card"],
            "outputs": {"diff": "text", "account": "text", "coding": "session"},
            "to": ["fix"],
        },
        "fix": {
            "type": "write",
            "role": "coder",
            "prompt": "Now write MORE after: {account}",
            "session": "coding",
            "inputs": ["coding", "account"],
            "outputs": {"diff.fix": "text", "account.fix": "text"},
        },
    }
    held: list[Any] = []

    async def write(node: Any, ctx: RunContext) -> Any:
        out = await write_runner(node, ctx, asker=pipe.asker, st=st)
        held.append(out.get("coding") or ctx.sessions.get("coding"))
        return out

    spec = WorkflowSpec.model_validate({"inputs": ["card"], "nodes": nodes})
    c = Composer(
        pipe.cfg,
        runners={"write": write},
        registry=Registry(),
        store=MemoryStore({"card": "add a marker"}),
        sessions=Sessions(),
        workflows={"w": spec},
    )
    await c.run("w")
    diff, later = c.store.get("diff"), c.store.get("diff.fix")
    assert "+++ b/PASS" in diff and "+ok" in diff and "MORE" not in diff
    assert "+++ b/PASS" in later and "+++ b/MORE" in later and "+more" in later
    assert c.store.get("account") == "done" and c.store.get("account.fix") == "added MORE"
    assert isinstance(held[0], Session) and held[1] is held[0]  # one session, continued
    assert len(st.runs) == 1 and c.sessions.get("coding") is None  # one studio run, closed at the end
    assert "Now write MORE after: done" in briefs_to(engine, "coder")[-1]


async def test_a_write_node_starts_from_the_head_of_the_worktree_its_input_names(tmp_path: Path) -> None:
    """A `ref:worktree` input: the write's own checkout is detached at that worktree's HEAD (its `run` event
    names the commit), so what was committed there is the start and not in the diff; nothing is left
    staged in the checkout the session goes on in."""
    repo = repo_with_marker_tests(tmp_path)
    tree = tmp_path / "other"
    git(repo, "worktree", "add", "-q", "-b", "other", str(tree))
    (tree / "OTHER").write_text("other\n")
    git(tree, "add", "OTHER")
    git(tree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "other")
    head = git(tree, "rev-parse", "HEAD")
    engine = happy_engine(coder=[WRITE_PASS, DONE], reviewer=[APPROVE] * 2)
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    nodes = {
        "place": {"type": "check", "run": "place", "outputs": {"wt": "ref:worktree"}, "to": ["build"]},
        "build": {
            "type": "write",
            "role": "coder",
            "prompt": "Write PASS.",
            "inputs": ["wt"],
            "outputs": {"diff": "text", "coding": "session"},
        },
    }
    staged: list[str] = []

    async def write(node: Any, ctx: RunContext) -> Any:
        out = await write_runner(node, ctx, asker=pipe.asker, st=st)
        checkout = out["coding"].state.tree
        staged.append(git(checkout, "diff", "--cached", "--name-only"))
        return out

    placed = Catalog()
    placed.add(Check("place", (), ("wt",), "Place", lambda ctx: {"wt": str(tree)}))
    registry = Registry(catalog=placed)
    c = Composer(
        pipe.cfg,
        runners={"write": write, "check": check_runner},
        registry=registry,
        store=MemoryStore(),
        sessions=Sessions(),
        workflows={"w": WorkflowSpec.model_validate({"nodes": nodes})},
    )
    await c.run("w")
    runs = [e for e in kinds_all(st.path / "events.jsonl") if e["kind"] == "run"]
    assert [e["base_sha"] for e in runs] == [head]
    diff = c.store.get("diff")
    assert "+++ b/PASS" in diff and "OTHER" not in diff
    assert staged == [""]  # the diff was read through a copy of the index


async def test_a_write_that_fails_continues_or_names_an_output_it_cannot_fill(tmp_path: Path) -> None:
    """`on_fail: continue`: a failed writing turn emits the node's `failed` event and the node fails without
    outputs. An output a write cannot fill (neither diff, account nor a session) is refused before any
    turn."""
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(coder=[])
    pipe = pipeline(tmp_path, repo, engine)
    st = bare_state(tmp_path, repo)
    node = v2(
        type="write",
        role="coder",
        prompt="Write PASS.",
        outputs={"diff": "text", "account": "text"},
        on_fail="continue",
        events={"failed": "build_failed"},
    )
    ctx = run_context(pipe, Registry(), MemoryStore(), "build")
    assert set(await write_runner(node, ctx, asker=pipe.asker, st=st)) == {FAIL_KEY}
    [failed] = [e for e in kinds_all(st.path / "events.jsonl") if e["kind"] == "build_failed"]
    assert failed["reason"] == "StoryError: the coder did not finish (engine_error)"
    asked = len(engine.requests)
    wrong = node.model_copy(update={"outputs": {"notes": OutputSpec(format="text")}})
    with pytest.raises(StoryError, match="output 'notes' is none of what a write produces"):
        await write_runner(wrong, ctx, asker=pipe.asker, st=st)
    assert len(engine.requests) == asked


async def test_a_conversation_nodes_events_name_its_legacy_kinds(tmp_path: Path) -> None:
    asker = TalkingAsker(['```json\n{"layers": ["a"]}\n```', "no", "no"])
    said: list[tuple[str, dict[str, Any]]] = []
    st = SimpleNamespace(runs=[], emit=lambda kind, **f: said.append((kind, f)))
    node = v2(
        type="conversation",
        role="architect",
        prompt="go",
        turn="again",
        steps=2,
        outputs=PLAN_FIELDS,
        on_fail="continue",
        events={"done": "layers_done", "failed": "layers_failed"},
    )
    ctx = RunContext(
        cfg=cfg_for(tmp_path),
        store=MemoryStore(),
        sessions=Sessions(),
        registry=Registry(),
        workflow="w",
        node="layers",
        item=None,
        feedback=None,
    )
    assert await conversation_runner(node, ctx, asker=cast(Any, asker), st=cast(Any, st)) == {
        "plan": {"layers": ["a"]}
    }
    assert set(await conversation_runner(node, ctx, asker=cast(Any, asker), st=cast(Any, st))) == {FAIL_KEY}
    reason = "StoryBlocked: architect did not produce a usable plan: the reply has no JSON object"
    assert said[0] == ("layers_done", {"chars": len('```json\n{"layers": ["a"]}\n```')})
    assert said[1][0] == "layers_failed" and said[1][1]["reason"].startswith(reason)
    assert len(said) == 2


def drafting_closed(st: StoryState) -> None:
    """The drafting conversation of `st` was closed when its run ended: its studio worktree is removed.
    (The composer's Sessions live inside `compose`, out of a test's reach: the worktree is what shows it.)"""
    runs = [e["studio_run"] for e in kinds_all(st.path / "events.jsonl") if e.get("stage") == "packages"]
    assert runs
    for run in runs:
        tree = st.path.parents[1] / "runs" / run / "studio"
        assert tree.parent.is_dir() and not tree.exists(), tree


async def test_a_package_retry_continues_the_drafting_session(tmp_path: Path) -> None:
    """story.layer_attempts 2, layer 01 red after its first build: its package retry is a turn of the
    architect's drafting conversation (`package_resumed`), which the composer holds as the `drafts_session`
    the package node names; the drafting is had once. The same events and state as the driver's."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        engine = FakeEngine(
            {
                "product": [STORY],
                "architect": [CALC_LAYERS, RED, OTHER_RED],
                "coder": [BROKEN, DONE, STILL_BROKEN, DONE, FEATURE, DONE],
                "reviewer": [APPROVE, APPROVE],
            }
        )
        repo = calc_repo(root)
        cmd = f"{shlex.quote(sys.executable)} -m pytest -q"
        pipe = pipeline(root, repo, engine, test_cmd=cmd, story={"layer_attempts": 2}, composer=composer)
        st = await pipe.run(text_card("multiply"))
        assert st.blocked is None and st.stage == "review", st.blocked
        events, state = built_run(st, root, composer=composer)
        kinds = [e["kind"] for e in events]
        assert kinds.count("packages_drafting") == 1 and kinds.count("package_resumed") == 1
        assert "drafts_session" not in json.loads((st.path / "state.json").read_text())  # never saved
        drafting_closed(st)  # the run ended at the review gate (EndRun)
        seen.append((events, state))
    assert seen[1] == seen[0]


async def test_a_lost_session_falls_back_to_a_fresh_package_ask(tmp_path: Path) -> None:
    """A story drafted and stopped on its layer's last build, then built again by a fresh pipeline: the
    drafting conversation went with the first process, so the layer's package asks afresh, its retry after
    a red build too (`package_start`, never `package_resumed`), and the story still reaches review. The
    same events and state as the driver's."""
    cmd = f"{shlex.quote(sys.executable)} -m pytest -q"
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = calc_repo(root)
        card = text_card("multiply")
        first = FakeEngine(
            {
                "product": [STORY],
                "architect": [CALC_LAYERS, RED],
                "coder": [BROKEN, DONE, STILL_BROKEN, DONE],
                "reviewer": [APPROVE],
            }
        )
        stopped = await pipeline(root, repo, first, test_cmd=cmd, composer=composer).run(card)
        assert stopped.blocked is not None and stopped.drafted and not stopped.built, stopped.blocked
        drafting_closed(stopped)  # the run ended blocked (StoryBlocked)
        log = stopped.path / "events.jsonl"
        before = len(log.read_text().splitlines())
        again = FakeEngine(
            {
                "architect": [RED, OTHER_RED],
                "coder": [BROKEN, DONE, STILL_BROKEN, DONE, FEATURE, DONE],
                "reviewer": [APPROVE, APPROVE],
            }
        )
        pipe = pipeline(root, repo, again, test_cmd=cmd, story={"layer_attempts": 2}, composer=composer)
        st = await pipe.build(card)
        assert st.blocked is None and st.stage == "review", st.blocked
        later = [e["kind"] for e in kinds_all(log)[before:]]
        assert later.count("package_start") == 2 and "package_resumed" not in later
        assert "packages_drafting" not in later
        seen.append(built_run(st, root, composer=composer))
    assert seen[1] == seen[0]


async def test_plan_rounds_are_turns_of_one_conversation(tmp_path: Path) -> None:
    """story.plan_round_steps 2, plan_max_rounds 2: the architect's first round has no plan ("more"), is
    sent back in the same conversation and plans, and round 2 converges on it. The round events (the fix,
    each round's plan, the steering, the end) are the driver's."""
    settings = {"plan_round_steps": 2, "plan_max_rounds": 2}
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        engine = happy_engine(architect=[FakeReply(text="more"), LAYERS, LAYERS])
        pipe = pipeline(root, repo, engine, story=settings, composer=composer)
        st = await pipe.plan(text_card("add a marker"))
        assert st.blocked is None and st.stage == "layers", st.blocked
        assert st.base is not None
        events = plan_events(st.path / "events.jsonl", root, st.base["sha"])
        rounds = [e["kind"] for e in events if str(e["kind"]).startswith("plan_") or e["kind"] == "steer"]
        assert rounds == ["plan_fix", "steer", "plan_round", "steer", "plan_round", "plan_done"]
        assert [e["kind"] for e in events].count("run") == 2  # refine, and one conversation for the rounds
        seen.append((events, plan_state(st.path, root, st.base["sha"], composer=composer)))
    assert seen[1] == seen[0]


@pytest.mark.parametrize("worth", [False, True])
async def test_a_plans_own_verdict_ends_the_rounds_early_either_way(tmp_path: Path, worth: bool) -> None:
    """A plan whose `refine` says another round is not worth it ends the planning rounds at once: a
    `plan_check` event, one `plan_round`, `plan_done` with `architect_done`. A plan that says another round
    is worth it goes on (here to round 2, which converges). Driver and composer alike, with the same
    events and state."""
    settings = {"plan_round_steps": 2, "plan_max_rounds": 2}
    verdict = {"worth_another_round": worth, "reason": "enough read", "check": ""}
    plan = [
        layer("storage", "Storage", "store.py", "AC1", "Store."),
        layer("profile", "Profile", "profile.py", "AC2", "Show."),
    ]
    first = FakeReply(text=fenced({"layers": plan, "refine": verdict}))
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        engine = happy_engine(architect=[first, LAYERS])
        pipe = pipeline(root, repo, engine, story=settings, composer=composer)
        st = await pipe.plan(text_card("add a marker"))
        assert st.blocked is None and st.stage == "layers", st.blocked
        assert st.base is not None
        events = plan_events(st.path / "events.jsonl", root, st.base["sha"])
        kinds = [e["kind"] for e in events]
        [check] = [e for e in events if e["kind"] == "plan_check"]
        assert check["worth_another_round"] is worth
        done = [e["reason"] for e in events if e["kind"] == "plan_done"]
        if worth:
            assert kinds.count("plan_round") == 2 and done == ["converged"]
        else:
            assert kinds.count("plan_round") == 1 and done == ["architect_done"]
        seen.append((events, plan_state(st.path, root, st.base["sha"], composer=composer)))
    assert seen[1] == seen[0]


# ---- the three amigos through the meeting runner, against the driver ----


def killed_after(n: int) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """A meeting's turn hook that saves the turn as the pipeline does, then kills the run after turn `n`."""
    turns: list[int] = []

    def hook(on_turn: Callable[..., None]) -> Callable[..., None]:
        def hooked(turn: Any, state: Any) -> None:
            on_turn(turn, state)
            turns.append(1)
            if len(turns) == n:
                raise asyncio.CancelledError

        return hooked

    return hook


@pytest.mark.parametrize("resume", [False, True])
async def test_the_meeting_runs_and_resumes_from_saved_state_the_same_as_the_driver(
    tmp_path: Path, resume: bool
) -> None:
    """With story.amigos on, driver and composer plan the same story through the meeting: the same events
    with the same fields in the same order and the same state.json. With `resume`, the first plan is killed
    right after the meeting's fifth turn (its team state saved) and a fresh pipeline plans again: the
    meeting goes on from the saved history (`amigos_start` resumed, six turns in all), driver and composer
    alike."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_levels(root)
        card = text_card("avatars")
        script: dict[str, list[str]] = EASY_MEETING
        engine = happy_engine()
        if resume:
            meeting = replay_meeting(EASY_MEETING, hook=killed_after(5))
            first = pipeline(
                root, repo, engine, story=MEETING, roles=MEETING_ROLES, meeting=meeting, composer=composer
            )
            with pytest.raises(asyncio.CancelledError):
                await first.plan(card)
            saved = StoryState.open(first.stories_dir / card.key, card, first.repo)
            assert saved.meeting is not None and saved.agreement is None
            # what each role has left to say: the architect's second turn
            script = {"product": [], "architect": EASY_MEETING["architect"][1:], "tester": []}
            engine = FakeEngine({})  # product is not asked to refine again, nor the architect to split
        pipe = pipeline(
            root,
            repo,
            engine,
            story=MEETING,
            roles=MEETING_ROLES,
            meeting=replay_meeting(script),
            composer=composer,
        )
        st = await pipe.plan(card)
        assert st.blocked is None and st.stage == "layers", st.blocked
        assert st.agreement is not None and st.meeting is None
        assert st.base is not None
        events = plan_events(st.path / "events.jsonl", root, st.base["sha"])
        starts = [e["resumed"] for e in events if e["kind"] == "amigos_start"]
        assert starts == ([False, True] if resume else [False])
        roles = [e["role"] for e in events if e["kind"] == "amigos_turn"]
        assert roles == ["product", "architect", "tester", "airlie", "product", "architect"]
        assert st.agreement["turns"] == 6
        seen.append((events, plan_state(st.path, root, st.base["sha"], composer=composer)))
    assert seen[1] == seen[0]


MEETING_NODE = {
    "type": "meeting",
    "role": ["product", "architect", "tester"],
    "prompt": "Agree on {story.title} before anyone builds.",
    "turn": "Speak for your part of {story.title}.",
    "inputs": ["story", "layers"],
    "outputs": {"agreement": "json", "story": "json", "layers": "list"},
}


async def test_the_meeting_runner_holds_its_meeting_from_the_saved_state_through_the_protocol(
    tmp_path: Path,
) -> None:
    """The generic runner: the protocol (`agree`) is called with a `Hold`, which makes the meeting from the
    factory (the tools over the checkout the protocol names, a fresh turn callback, the wait before each
    turn, the node's rendered turn text) and holds it from the team state the store has saved, and with the
    node's rendered prompt as the opening text; the node's declared outputs are what the protocol returns."""
    pipe = pipeline(tmp_path, repo_with_marker_tests(tmp_path), happy_engine())
    node = v2(**MEETING_NODE)
    made: list[Any] = []
    turned: list[Any] = []

    async def wait() -> None:
        pass

    class Held:
        before_turn: Any = None
        turn_text: Any = None

        async def run(self, opening: str, *args: Any, repo: Path, base: str, state: Any = None) -> Any:
            made.append((opening, args, repo, base, state, self.before_turn, self.turn_text))
            self_on_turn("turn", {"n": 1})
            return "the agreement"

    def factory(cfg: Any, tools: Any, on_turn: Any) -> Any:
        nonlocal self_on_turn
        self_on_turn = on_turn
        made.append((cfg is pipe.cfg, tools("tester")))
        return Held()

    self_on_turn: Any = None

    async def agree(ctx: RunContext, hold: Hold, opening: str | None) -> dict[str, Any]:
        agreed = await hold(
            tmp_path / "tree", opening or "", "story", "layers", {}, set(), repo=tmp_path, base="b"
        )
        return {"agreement": agreed, "story": "reworded", "stray": "not an output"}

    store = MemoryStore({"meeting": {"saved": True}, "story": {"title": "Avatars"}, "layers": []})
    out = await meeting_runner(
        node,
        run_context(pipe, Registry(), store, "amigos"),
        agree=agree,
        meeting=factory,
        tools=lambda tree: lambda role: f"{role} reads {tree.name}",
        on_turn=lambda: lambda turn, state: turned.append((turn, state)),
        before_turn=lambda: wait,
        roles=("product", "architect", "tester"),
    )
    assert out == {"agreement": "the agreement", "story": "reworded"}
    opening = "Agree on Avatars before anyone builds."
    assert made == [
        (True, "tester reads tree"),
        (
            opening,
            ("story", "layers", {}, set()),
            tmp_path,
            "b",
            {"saved": True},
            wait,
            "Speak for your part of Avatars.",
        ),
    ]
    assert turned == [("turn", {"n": 1})]
    with pytest.raises(ValueError, match="product, architect, tester"):  # the protocol's seats
        await meeting_runner(
            node.model_copy(update={"role": ["product", "coder"]}),
            run_context(pipe, Registry(), store, "amigos"),
            agree=agree,
            meeting=factory,
            tools=lambda tree: lambda role: role,
            on_turn=lambda: lambda turn, state: None,
            roles=("product", "architect", "tester"),
        )


async def test_a_meeting_nodes_prompt_and_turn_texts_reach_the_amigos_opening_and_every_seat(
    tmp_path: Path,
) -> None:
    """The story's meeting runner over the amigos protocol (`_agree`): the node's rendered prompt replaces the
    hub's `amigos-opening` text at the head of the opening message (the story, the split and the contracts
    follow as before), its rendered turn text replaces `amigos-turn` in each seat's system message, and the
    agreement rewrites the story and the layers."""
    repo = repo_with_levels(tmp_path)
    seen: list[tuple[str, str]] = []
    replay = replay_meeting(EASY_MEETING)

    def recording(cfg: Any, tools: Any, on_turn: Any) -> Any:
        meeting = replay(cfg, tools, on_turn)
        run = meeting.run

        async def run_and_see(opening: str, *args: Any, **kwargs: Any) -> Any:
            agent, client = meeting._agent("tester")
            seen.append((opening, agent._system_messages[0].content))
            await client.close()
            return await run(opening, *args, **kwargs)

        meeting.run = run_and_see  # type: ignore[method-assign]
        return meeting

    pipe = pipeline(tmp_path, repo, happy_engine(), story=MEETING, roles=MEETING_ROLES, meeting=recording)
    card = text_card("avatars")
    st = StoryState.open(pipe.stories_dir / card.key, card, pipe.repo)
    st.base = {"ref": "HEAD", "sha": git(repo, "rev-parse", "HEAD")}
    story, split = Story.from_dict(REWORDED_STORY), AGREED_SPLIT["layers"]
    store = MemoryStore({"story": story.to_dict(), "layers": split})
    runner = story_runners(pipe, st, card)["meeting"]
    out = await runner(v2(**MEETING_NODE), run_context(pipe, Registry(), store, "amigos"))
    assert st.agreement is not None and out == {
        "agreement": st.agreement,
        "story": st.story,
        "layers": st.layers,
    }
    assert isinstance(out["agreement"], dict) and isinstance(out["story"], dict)  # what the state holds
    assert all(isinstance(x, dict) for x in out["layers"])
    [(opening, system)] = seen
    assert opening.startswith("Agree on Add avatar upload before anyone builds.\n\n## Product's story")
    assert hub_text("amigos-opening") not in opening
    assert "Speak for your part of Add avatar upload." in system and hub_text("amigos-turn") not in system
    assert opening_message(story, tuple(Layer.from_dict(x) for x in split), "c", []).startswith(
        hub_text("amigos-opening")
    )  # without a node's text, the opening is the hub's as before


async def test_the_gate_runner_calls_its_run_piece_with_the_nodes_args_then_ends_the_run(
    tmp_path: Path,
) -> None:
    """The generic runner: the node's `run` resolved in the registry's catalog before the call, called as
    `fn(ctx, **node.args)` (it may be async), and then the run ends (`EndRun`), whatever it returned."""
    pipe = pipeline(tmp_path, repo_with_marker_tests(tmp_path), happy_engine())
    node = v2(type="gate", run="story.finish", args={"to": "review"})
    called: list[Any] = []

    async def piece(ctx: RunContext, **args: Any) -> None:
        called.append((ctx.node, args))

    finishing = Catalog()
    finishing.add(Check("story.finish", (), (), "Finish", piece))
    registry = Registry(catalog=finishing)
    ctx = run_context(pipe, registry, MemoryStore(), "review")
    with pytest.raises(EndRun):
        await gate_runner(node, ctx)
    assert called == [("review", {"to": "review"})]
    with pytest.raises(KeyError):  # a wrong id is a config error, raised before anything is called
        await gate_runner(node.model_copy(update={"run": "story.nope"}), ctx)
    assert len(called) == 1


async def test_the_review_gate_ends_the_run_and_releases_the_build_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through `run` with the composer on, holding the build lane as the line does: the review gate posts the
    links and moves the card, the composer's run returns at the gate's name, `_drive` returns normally (the
    stage `review`, then `story_done`), its `finally` writes the idle phase, and the lane is free after."""
    returned: list[Any] = []
    run = Composer.run

    async def spy(self: Composer, *args: Any, **kwargs: Any) -> Any:
        returned.append(await run(self, *args, **kwargs))
        return returned[-1]

    monkeypatch.setattr(Composer, "run", spy)
    pipe = pipeline(tmp_path, repo_with_marker_tests(tmp_path), happy_engine(), composer=True)
    with lane_lock(pipe.stories_dir, "build") as held:
        assert held
        st = await pipe.run(text_card("add a marker"))
        assert lock_held(pipe.stories_dir, "build") is True  # still ours: the run neither took nor freed it
    assert st.blocked is None and st.stage == "review" and st.done("review"), st.blocked
    assert returned == ["review"]
    events = kinds_all(st.path / "events.jsonl")
    story = [e for e in events if not e["kind"].startswith("node_")]
    assert [(e["kind"], e.get("stage")) for e in story[-2:]] == [
        ("stage", "review"),
        ("story_done", "review"),
    ]
    # the layer bundle has a `review` node too: the gate is the story's
    finished = [e for e in events if e["kind"] == "node_finished"]
    gate = [e for e in finished if (e["node"], e["type"]) == ("review", "gate")]
    assert [e["outcome"] for e in gate] == ["done"]
    assert json.loads(phase_path(pipe.stories_dir).read_text())["phase"] == "idle"
    assert lock_held(pipe.stories_dir, "build") is False


async def test_a_build_after_the_review_gate_resumes_after_it_the_same_either_way(tmp_path: Path) -> None:
    """Review Focus 5's other half: a fresh pipeline's `build` of a story the review gate finished returns at
    once at stage review, building nothing, with the driver's events (a resumed `story_start`, then
    `story_done`), composer or not."""
    seen = []
    for composer in (False, True):
        root = tmp_path / ("composer" if composer else "driver")
        root.mkdir()
        repo = repo_with_marker_tests(root)
        card = text_card("add a marker")
        first = await pipeline(root, repo, happy_engine(), composer=composer).run(card)
        assert first.blocked is None and first.stage == "review", first.blocked
        before = len(kinds_all(first.path / "events.jsonl"))
        st = await asyncio.wait_for(pipeline(root, repo, happy_engine(), composer=composer).build(card), 30)
        assert st.blocked is None and st.stage == "review", st.blocked
        again = kinds_all(st.path / "events.jsonl")[before:]
        story = [
            (e["kind"], e.get("stage"), e.get("resumed")) for e in again if not e["kind"].startswith("node_")
        ]
        assert story[0] == ("story_start", "review", True) and story[-1][:2] == ("story_done", "review")
        assert not {"layer_start", "package_start", "layer_built", "published"} & {k for k, _, _ in story}
        assert any(e["kind"] == "node_started" for e in again) is composer
        seen.append(story)
    assert seen[1] == seen[0]


# ---- a card on another workflow (D22, 2026-11-07) ----


def test_a_card_on_another_workflow_is_drawn_from_its_nodes(tmp_path: Path) -> None:
    from tests.support.flow_nodes import EVENTS, bugfix

    story = tmp_path / "stories" / "AIR-9"
    story.mkdir(parents=True)
    st = {"stage": "new", "card": {"key": "AIR-9", "title": "Fix the bug"}, **bugfix()}
    (story / "state.json").write_text(json.dumps(st))
    write_events(story / "events.jsonl", EVENTS)
    flow = story_flow(tmp_path, "AIR-9", held=True)
    ids = [n["id"] for n in flow["nodes"]]
    assert ids == ["intake", "reproduce", "fix_layers:01", "fix_layers:02", "review"]
    assert flow["title"] == "Fix the bug"
    assert flow["now"]["status"] == "running" and flow["now"]["node"] == "fix_layers:02"
    assert flow["now"]["job"] == "write"
    quiet = story_flow(tmp_path, "AIR-9", now=10_000.0)  # nobody works on it: the running box is stopped
    assert by_id(quiet["nodes"], "fix_layers:02")["status"] == "stopped"


def test_a_card_on_the_story_workflow_keeps_the_story_screen(state_dir: Path) -> None:
    from tests.support.flow_nodes import bugfix

    f = state_dir / "stories" / KEY / "state.json"
    snap = bugfix()["workflow"]
    f.write_text(json.dumps({**json.loads(f.read_text()), "workflow": {**snap, "name": "story"}}))
    ids = [n["id"] for n in story_flow(state_dir, KEY, held=True)["nodes"]]
    assert ids[:3] == ["intake", "base", "gate"] and "layer-01" in ids
