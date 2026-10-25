"""The flow of a story as the pipeline page draws it: stages left to right, a lane of jobs per layer, each job
with its status, attempts, tokens and speed, derived from the story's state.json and events and the runs they
name."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.serve.flow import StoryNotFound, epic_flow, job_detail, story_flow
from tests.unit.test_story_epic import fenced
from tests.unit.test_story_epic_pipeline import EPIC_TEXT, SPEC, plan_obj

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


# ---- an epic (P3, 2026-10-24) ----


def write_epic(root: Path, key: str = "AIR-1", *, damaged: bool = False) -> Path:
    """Epic `key` under `root`: planned into S1 (AIR-2, in review) and S2 (AIR-3, not started), with one
    re-plan on record and a story it left out (AIR-9)."""
    d = root / "epics" / key
    d.mkdir(parents=True)
    if damaged:
        (d / "state.json").write_text("{ not json")
        return root
    state = {
        "card": {"key": key, "title": "Avatars", "description": EPIC_TEXT + fenced(SPEC.to_dict())},
        "repo": "o/r",
        "stage": "building",
        "plan": plan_obj(),
        "attempts": 2,
        "stories": {"S1": "AIR-2", "S2": "AIR-3", "S0": "AIR-9"},
        "replans": [{"reason": "AIR-9 stopped again", "created": ["AIR-3"], "retired": ["AIR-9"]}],
        "blocked": None,
        "runs": [],
        "base": {"ref": "origin/main", "sha": "a" * 40},
        "seen_comments": 0,
    }
    (d / "state.json").write_text(json.dumps(state))
    (d / "events.jsonl").write_text("")
    story = root / "stories" / "AIR-2"
    story.mkdir(parents=True)
    (story / "state.json").write_text(
        json.dumps({"card": {"key": "AIR-2"}, "stage": "review", "blocked": None})
    )
    return root


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


def delivered_initiative(tmp_path: Path) -> tuple[Any, str, str, str]:
    """A board with an initiative whose core and extension are carded (the core with three mockup pages, one
    picked) and its record on disk: returns the store, the initiative's key and the two epics' keys."""
    from airlie.board import BoardStore
    from airlie.story.costs import Estimate
    from airlie.story.epic import EpicCriterion, EpicSpec, Mockups, Viability, with_block

    store = BoardStore(tmp_path / "board.sqlite")
    init = store.create(
        level="initiative", title="Linear feel", description="Faster.", labels=["repo:o/r", "budget:20h"]
    ).key
    urls = tuple(f"/v1/airlie/initiatives/{init}/mockups/E1/option-{n}.html" for n in (1, 3))
    core_spec = EpicSpec(
        ("o/r",),
        (EpicCriterion("EC1", "a card moves"),),
        (),
        urls,
        init,
        "core",
        estimate=Estimate(5.0, "1 M at 5.0 h (default)", "2026-10-25T00:00:00Z", revised=6.0),
        viability=Viability("risky", ("old store",)),
        mockups=Mockups(urls, 3),
    )
    core = store.create(
        level="epic",
        title="Fast board",
        parent=init,
        description=with_block("# Fast board\n", core_spec),
        labels=[f"initiative:{init}"],
    ).key
    ext_spec = EpicSpec(
        ("o/r",),
        (EpicCriterion("EC1", "cards are cropped"),),
        initiative=init,
        kind="extension",
        depends_on=(core,),
        estimate=Estimate(2.0, "2 S at 1.0 h (default)", "2026-10-25T00:00:00Z"),
        viability=Viability("viable", ("small",)),
    )
    ext = store.create(
        level="epic",
        title="Crops",
        parent=init,
        description=with_block("# Crops\n", ext_spec),
        labels=[f"initiative:{init}", f"after:{core}"],
    ).key
    store.update(core, status="approved")  # approved and not done: its hours are in the queue
    store.update(ext, status="review")
    folder = tmp_path / "initiatives" / init
    folder.mkdir(parents=True)
    state = {
        "card": {"key": init, "title": "Linear feel"},
        "stage": "delivered",
        "summary": "Make the board fast.",
        "assumptions": ["one user"],
        "qa": [
            {"q": "What is it for?", "asked_at": 1.0, "a": "Speed.", "answered_at": 2.0},
            {"q": "Who?", "asked_at": 3.0, "a": None, "answered_at": None},
        ],
        "consultations": [
            {"seat": "ux", "question": "How?", "answer": "- keys", "seconds": 12.3, "round": 1, "chars": 6},
        ],
        "epics": [
            {
                "id": "E1",
                "key": core,
                "kind": "core",
                "title": "Fast board",
                "viability": {"verdict": "risky"},
            },
            {"id": "E2", "key": ext, "kind": "extension", "title": "Crops"},
            {"id": "E3", "kind": "extension", "title": "Not carded yet"},
        ],
    }
    (folder / "state.json").write_text(json.dumps(state))
    epics_dir = tmp_path / "epics" / core
    epics_dir.mkdir(parents=True)
    (epics_dir / "state.json").write_text(
        json.dumps({"estimate": {"hours": 5.0, "basis": "b", "at": "t", "revised": 6.0}})
    )
    return store, init, core, ext


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
