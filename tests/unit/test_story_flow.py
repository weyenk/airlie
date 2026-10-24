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


def test_a_layer_is_a_lane_of_jobs_with_tester_and_coder_stats(state_dir: Path) -> None:
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
