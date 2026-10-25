"""The flow of a story as the pipeline page draws it: stages left to right, a lane of jobs per layer, each job
with its status, attempts, tokens and speed, derived from the story's state.json and events and the runs they
name."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.serve.flow import StoryNotFound, job_detail, story_flow

KEY = "AIR-1"


def write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def step(run: str, team: str, n: int, *, completion: int = 100, gen_ms: float = 2000.0) -> dict[str, Any]:
    return {
        "ts": 1.0,
        "run_id": run,
        "kind": "step",
        "team_id": team,
        "step": n,
        "tool": "read_file",
        "ok": True,
        "prompt_tokens": 1000,
        "completion_tokens": completion,
        "prefill_ms": 500.0,
        "gen_ms": gen_ms,
    }


def write_story(root: Path) -> Path:
    """Story KEY under `root`: refined and planned in two layers, layer 01 built, layer 02 blocked on its
    tests; the studio runs its stages and layers name, and the tester's trace of layer 01. Returns `root`."""
    story = root / "stories" / KEY
    story.mkdir(parents=True)
    layers = [
        {
            "slug": "seam",
            "title": "The seam",
            "scope": "src/a.py",
            "brief": "build the seam",
            "acceptance": ["AC1"],
        },
        {"slug": "wire", "title": "Wire it", "scope": "src/b.py", "brief": "wire the seam", "acceptance": []},
    ]
    state = {
        "card": {"key": KEY, "title": "Do the thing"},
        "repo": "/r",
        "stage": "layers",
        "story": {
            "title": "Do the thing",
            "summary": "who wants what",
            "acceptance": [{"id": "AC1", "given": "g", "when": "w", "then": "t"}],
        },
        "layers": layers,
        "built": {"01": {"branch": "b1", "sha": "abc", "run_id": "run-l1"}},
        "blocked": "layer 02 (Wire it): its tests still do not test its change after one retry",
        "runs": ["run-refine", "run-l1", "run-l2"],
    }
    (story / "state.json").write_text(json.dumps(state))
    write_events(
        story / "events.jsonl",
        [
            {"ts": 10.0, "kind": "story_start", "stage": "new"},
            {"ts": 11.0, "kind": "stage", "stage": "intake"},
            {"ts": 12.0, "kind": "run", "stage": "refine", "studio_run": "run-refine"},
            {"ts": 20.0, "kind": "retry", "stage": "refine", "error": "no acceptance criteria"},
            {"ts": 40.0, "kind": "stage", "stage": "refine"},
            {"ts": 41.0, "kind": "run", "stage": "layers", "studio_run": "run-layers"},
            {"ts": 90.0, "kind": "stage", "stage": "layers"},
            {"ts": 91.0, "kind": "layer_start", "layer": "01", "studio_run": "run-l1"},
            {"ts": 150.0, "kind": "test_checks_passed", "layer": "01", "attempt": 1},
            {"ts": 300.0, "kind": "layer_built", "layer": "01", "branch": "b1", "sha": "abc"},
            {"ts": 301.0, "kind": "layer_start", "layer": "02", "studio_run": "run-l2"},
            {"ts": 340.0, "kind": "no_new_test", "layer": "02", "attempt": 1, "why": "no new test"},
            {"ts": 341.0, "kind": "test_checks_retry", "layer": "02", "findings": 1},
            {"ts": 380.0, "kind": "no_new_test", "layer": "02", "attempt": 2, "why": "no new test"},
            {"ts": 381.0, "kind": "blocked", "stage": "layers", "reason": "layer 02 stopped"},
        ],
    )
    runs = root / "runs"
    write_events(
        runs / "run-refine" / "events.jsonl",
        [step("run-refine", "refine", 1), step("run-refine", "refine", 2)],
    )
    write_events(runs / "run-layers" / "events.jsonl", [step("run-layers", "layers", 1)])
    write_events(
        runs / "run-l1" / "events.jsonl",
        [
            {"ts": 91.0, "kind": "subtask_start", "subtask": "tests", "role": "tester"},
            step("run-l1", "tests", 1, completion=200, gen_ms=4000.0),
            {
                "ts": 150.0,
                "kind": "subtask_done",
                "subtask": "tests",
                "role": "tester",
                "ok": True,
                "stop_reason": "done",
                "steps": 1,
                "fix_rounds": 0,
            },
            {"ts": 151.0, "kind": "subtask_start", "subtask": "build", "role": "coder"},
            step("run-l1", "build", 1),
            {
                "ts": 290.0,
                "kind": "subtask_done",
                "subtask": "build",
                "role": "coder",
                "ok": True,
                "stop_reason": "done",
                "steps": 1,
                "fix_rounds": 1,
            },
            {
                "ts": 291.0,
                "kind": "review",
                "subtask": "build",
                "verdict": "approve",
                "findings": 0,
                "round": 2,
            },
            {"ts": 295.0, "kind": "run_done", "ok": True, "tests": True},
        ],
    )
    write_events(
        runs / "run-l2" / "events.jsonl",
        [
            {"ts": 301.0, "kind": "subtask_start", "subtask": "tests", "role": "tester"},
            {
                "ts": 340.0,
                "kind": "subtask_done",
                "subtask": "tests",
                "role": "tester",
                "ok": True,
                "stop_reason": "done",
                "steps": 3,
                "fix_rounds": 0,
            },
        ],
    )
    trace = runs / "run-l1" / "trace" / "tests"
    trace.mkdir(parents=True)
    (trace / "prompt.json").write_text(
        json.dumps({"role": "tester", "attempt": 0, "messages": [{"role": "user", "content": "write tests"}]})
    )
    (trace / "step-001.json").write_text(
        json.dumps(
            {
                "u_t": {"name": "read_file", "arguments": "{}"},
                "o_t": "x",
                "ok": True,
                "assistant_text": "",
                "reasoning": "think",
            }
        )
    )
    return root


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return write_story(tmp_path)


def by_id(items: list[dict[str, Any]], id: str) -> dict[str, Any]:
    (found,) = [x for x in items if x["id"] == id]
    return found


def test_the_flow_lists_the_stages_in_order_with_their_status(state_dir: Path) -> None:
    flow = story_flow(state_dir, KEY)
    assert flow["key"] == KEY and flow["title"] == "Do the thing"
    assert [n["id"] for n in flow["nodes"]] == [
        "intake", "refine", "survey", "layers", "layer-01", "layer-02", "publish", "review",
    ]  # fmt: skip
    status = {n["id"]: n["status"] for n in flow["nodes"]}
    assert status["intake"] == status["refine"] == status["layers"] == status["layer-01"] == "passed"
    assert status["layer-02"] == "failed" and status["publish"] == status["review"] == "pending"
    assert status["survey"] == "skipped"  # it never ran


def test_the_acceptance_criteria_and_the_block_reason_come_with_the_flow(state_dir: Path) -> None:
    flow = story_flow(state_dir, KEY)
    assert flow["acceptance"] == [{"id": "AC1", "given": "g", "when": "w", "then": "t"}]
    assert flow["blocked"].startswith("layer 02")


def test_a_stage_job_counts_its_retries_its_time_and_its_tokens(state_dir: Path) -> None:
    refine = by_id(story_flow(state_dir, KEY)["nodes"], "refine")
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
    layer = by_id(story_flow(state_dir, KEY)["nodes"], "layer-01")
    assert layer["title"] == "The seam" and layer["acceptance"] == ["AC1"]
    assert [j["id"] for j in layer["jobs"]] == ["tests", "checks", "build"]
    tests, checks, build = layer["jobs"]
    assert tests["role"] == "tester" and tests["status"] == "passed"
    assert tests["tokens"]["tps"] == pytest.approx(50.0)
    assert checks["status"] == "passed" and checks["attempts"] == 1
    assert build["role"] == "coder" and build["status"] == "passed" and build["fix_rounds"] == 1


def test_a_layer_that_stopped_shows_which_job_failed_and_why(state_dir: Path) -> None:
    layer = by_id(story_flow(state_dir, KEY)["nodes"], "layer-02")
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
    tests, _, build = by_id(story_flow(state_dir, KEY)["nodes"], "layer-02")["jobs"]
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
    layer = by_id(story_flow(state_dir, KEY2)["nodes"], "layer-01")
    assert [j["id"] for j in layer["jobs"]] == ["package", "checks", "build"]
    package, checks, build = layer["jobs"]
    assert (package["role"], package["status"], package["attempts"]) == ("architect", "passed", 1)
    assert package["studio_run"] == "run-p1" and package["tokens"]["tps"] == pytest.approx(50.0)
    assert package["duration_s"] == pytest.approx(60.0)  # from package_start to package_ready
    assert checks["status"] == "passed" and checks["retries"] == []
    assert build["role"] == "coder" and build["status"] == "passed"


def test_a_layer_whose_package_came_back_twice_shows_the_package_job_failed(state_dir: Path) -> None:
    write_packaged_story(state_dir)
    layer = by_id(story_flow(state_dir, KEY2)["nodes"], "layer-02")
    package, checks, build = layer["jobs"]
    assert (package["status"], package["attempts"]) == ("failed", 2)
    assert checks["status"] == "failed" and checks["attempts"] == 2
    assert [r["kind"] for r in checks["retries"]] == [
        "passes_before_code",
        "package_returned",
        "import_unresolved",
        "package_returned",
    ]
    assert build["status"] == "pending"
    assert layer["status"] == "failed"


def test_a_package_that_is_being_written_is_running(state_dir: Path) -> None:
    write_packaged_story(state_dir)
    events = state_dir / "stories" / KEY2 / "events.jsonl"
    lines = events.read_text().splitlines()
    events.write_text("\n".join(lines[:-3]) + "\n")  # layer 02: attempt 1 returned, attempt 2 started
    state = json.loads((state_dir / "stories" / KEY2 / "state.json").read_text())
    state["blocked"] = None
    (state_dir / "stories" / KEY2 / "state.json").write_text(json.dumps(state))
    package, *_ = by_id(story_flow(state_dir, KEY2)["nodes"], "layer-02")["jobs"]
    assert package["status"] == "running"


def test_the_package_job_detail_gives_the_architects_prompt(state_dir: Path) -> None:
    write_packaged_story(state_dir)
    detail = job_detail(state_dir, KEY2, "layer-01", "package")
    assert detail["input"]["messages"][0]["content"] == "package"


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
    layers = by_id(story_flow(state_dir, KEY)["nodes"], "layers")
    assert [j["id"] for j in layers["jobs"]] == ["layers", "meeting"]
    meeting = by_id(layers["jobs"], "meeting")
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
    meeting = by_id(by_id(story_flow(state_dir, KEY)["nodes"], "layers")["jobs"], "meeting")
    assert (
        meeting["status"] == "passed" and meeting["owner_decided"] == ["proof"] and meeting["unresolved"] == 1
    )


def test_a_meeting_in_progress_is_running_and_one_the_story_stopped_in_is_failed(state_dir: Path) -> None:
    add_events(state_dir, MEETING_EVENTS)
    state_file = state_dir / "stories" / KEY / "state.json"
    state = json.loads(state_file.read_text())
    state.update(stage="refine", blocked=None, built={})
    state_file.write_text(json.dumps(state))
    layers = by_id(story_flow(state_dir, KEY)["nodes"], "layers")
    assert by_id(layers["jobs"], "meeting")["status"] == "running" and layers["status"] == "running"
    state["blocked"] = "the engine went away"
    state_file.write_text(json.dumps(state))
    layers = by_id(story_flow(state_dir, KEY)["nodes"], "layers")
    assert by_id(layers["jobs"], "meeting")["status"] == "failed"


def test_a_story_without_a_meeting_has_no_meeting_job_and_the_job_has_a_detail(state_dir: Path) -> None:
    assert [j["id"] for j in by_id(story_flow(state_dir, KEY)["nodes"], "layers")["jobs"]] == ["layers"]
    add_events(state_dir, [*MEETING_EVENTS, {"ts": 85.0, "kind": "amigos_agreed", "turns": 4}])
    state_file = state_dir / "stories" / KEY / "state.json"
    state = json.loads(state_file.read_text())
    state["agreement"] = {"turns": 4, "proof": []}
    state_file.write_text(json.dumps(state))
    detail = job_detail(state_dir, KEY, "layers", "meeting")
    assert detail["output"]["agreement"] == {"turns": 4, "proof": []} and "meeting" in detail["input_note"]
