"""The build lane of a story's flow: drafting every layer's package, a lane per layer with a row per try cycle
(Package, checks, place, Build, squash) and a divider at each stop and resume, the Build's inner steps, then
publish and review. AIR-47's and AIR-48's real logs (trimmed, under tests/fixtures/flow) are the cases."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from airlie.serve.flow import story_flow
from airlie.serve.flow_build import cycles
from tests.unit.test_story_flow import KEY, by_id, write_events
from tests.unit.test_story_flow_plan import make_story

FIXTURES = Path(__file__).parent.parent / "fixtures" / "flow"
BUILD_JOBS = ["package", "checks", "place", "build", "squash"]


def from_fixture(root: Path, key: str) -> Path:
    """The trimmed real story `key` under `root` as airlie keeps it: its state and log, and its runs."""
    src = FIXTURES / key
    story = root / "stories" / key
    story.mkdir(parents=True)
    for f in ("state.json", "events.jsonl"):
        shutil.copy(src / f, story / f)
    if (src / "runs").is_dir():
        shutil.copytree(src / "runs", root / "runs", dirs_exist_ok=True)
    return root


def story_files(root: Path, key: str) -> tuple[Path, Path]:
    d = root / "stories" / key
    return d / "state.json", d / "events.jsonl"


def air47_at_16_12(root: Path) -> Path:
    """AIR-47 as it stood at 16:12 on 2026-11-02: L01 and L02 drafted, L03's turn out (its log is from before
    package_draft_start), nothing built yet."""
    from_fixture(root, "AIR-47")
    state, log = story_files(root, "AIR-47")
    lines = log.read_text().splitlines()
    drafted = [i for i, x in enumerate(lines) if json.loads(x)["kind"] == "package_drafted"]
    log.write_text("\n".join(lines[: drafted[1] + 1]) + "\n")
    st = json.loads(state.read_text())
    st.update(drafted=False, drafts={}, built={}, blocked=None)
    state.write_text(json.dumps(st))
    return root


def nodes_of(root: Path, key: str = KEY) -> list[dict[str, Any]]:
    return story_flow(root, key, held=True)["nodes"]


def steps_of(box: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in box["card"]["rows"] if r["kind"] == "step"]


def texts_of(box: dict[str, Any]) -> list[str]:
    return [r["text"] for r in box["card"]["rows"] if r["kind"] == "text"]


LAYERS = [
    {"slug": f"l{n}", "title": f"Layer {n}", "scope": "s", "acceptance": []} for n in ("01", "02", "03")
]


def layered(root: Path, events: list[dict[str, Any]], **state: Any) -> Path:
    return make_story(root, events, stage="layers", layers=LAYERS, **state)


# ---- the build lane's nodes ----


def test_the_build_lane_drafts_then_builds_each_layer_then_publishes_and_reviews(tmp_path: Path) -> None:
    layered(tmp_path, [{"ts": 1.0, "kind": "story_start", "stage": "new"}])
    nodes = nodes_of(tmp_path)
    build = [n for n in nodes if n.get("lane") == "build"]
    assert [n["id"] for n in build] == ["packages", "layer-01", "layer-02", "layer-03", "publish", "review"]
    (packages,) = by_id(nodes, "packages")["jobs"]
    assert (packages["label"], packages["shape"]) == ("Draft packages", "model")
    layer = by_id(nodes, "layer-01")
    assert layer["kind"] == "layer" and layer["rows"] == [] and layer["attempts"] == 0
    assert [j["id"] for j in layer["jobs"]] == BUILD_JOBS
    assert [j["shape"] for j in layer["jobs"]] == ["model", "code", "code", "model", "code"]
    assert {j["status"] for j in layer["jobs"]} == {"pending"}
    for n in build:
        for j in n["jobs"]:
            assert {"shape", "live", "tags", "card"} <= set(j)


# ---- drafting every package ----


def test_drafting_lights_the_running_turn(tmp_path: Path) -> None:
    nodes = nodes_of(air47_at_16_12(tmp_path), "AIR-47")
    job = by_id(nodes, "packages")["jobs"][0]
    assert job["status"] == "running"
    assert [r["status"] for r in steps_of(job)] == ["passed", "passed", "running", "pending", "pending"]
    assert (
        job["live"] == "L03 · package"
    )  # an old log: no package_draft_start, the turn after the last drafted
    assert steps_of(job)[0]["text"] == "L01 · package → critic agree"
    assert job["studio_run"] == "20261102-151618-a040"
    assert by_id(nodes, "layer-01")["rows"] == []  # drafted, not yet taken by its layer
    assert {"text": "drafted", "tone": "ok"} in by_id(nodes, "layer-01")["tags"]


def test_a_drafting_turn_with_its_start_logged_is_the_running_one(tmp_path: Path) -> None:
    events = [
        {"ts": 1.0, "kind": "packages_drafting", "layers": 3},
        {"ts": 1.0, "kind": "run", "stage": "packages", "studio_run": "run-d"},
        {"ts": 2.0, "kind": "package_draft_start", "layer": "01"},
    ]
    layered(tmp_path, events)
    job = by_id(nodes_of(tmp_path), "packages")["jobs"][0]
    assert [r["status"] for r in steps_of(job)] == ["running", "pending", "pending"]
    assert job["live"] == "L01 · package"
    write_events(
        tmp_path / "stories" / KEY / "events.jsonl",
        [*events, {"ts": 9.0, "kind": "run", "stage": "critic", "layer": "01", "studio_run": "run-c"}],
    )
    assert by_id(nodes_of(tmp_path), "packages")["jobs"][0]["live"] == "L01 · critic"


def test_a_drafting_stopped_by_an_unusable_reply_marks_that_turn_failed(tmp_path: Path) -> None:
    layered(
        tmp_path,
        [
            {"ts": 1.0, "kind": "packages_drafting", "layers": 3},
            {"ts": 2.0, "kind": "package_draft_start", "layer": "01"},
            {"ts": 3.0, "kind": "package_drafted", "layer": "01", "studio_run": "run-d"},
            {"ts": 4.0, "kind": "package_draft_start", "layer": "02"},
        ],
        drafted=True,
        drafts={"01": {"package": {}, "run": "run-d"}, "02": {"unusable": "no JSON object in the reply"}},
    )
    job = by_id(nodes_of(tmp_path), "packages")["jobs"][0]
    assert [r["status"] for r in steps_of(job)] == ["passed", "failed", "pending"]
    assert job["status"] == "passed" and job["live"] is None  # drafting ended


def test_air47_now_takes_l03s_unusable_draft_as_its_first_try(tmp_path: Path) -> None:
    nodes = nodes_of(from_fixture(tmp_path, "AIR-47"), "AIR-47")
    job = by_id(nodes, "packages")["jobs"][0]
    assert job["status"] == "passed"
    assert [r["status"] for r in steps_of(job)] == ["passed", "passed", "failed", "pending", "pending"]
    l03 = by_id(nodes, "layer-03")
    # the unusable draft is the layer's first try (L02's block in between is no stop of L03's): asked again
    # when its turn came, that is its second
    (only,) = l03["rows"]
    package = only["jobs"][0]
    assert only["kind"] == "cycle" and only["label"] == "package"
    assert package["attempts"] == 2 and package["status"] == "running"
    assert [r["text"] for r in steps_of(package)][:2] == [
        "try 1 · returned: package_unusable",
        "try 2 · architect",
    ]
    assert package["live"] == "try 2 · architect" and l03["status"] == "running"
    l02 = by_id(nodes, "layer-02")
    assert [r["kind"] for r in l02["rows"]] == ["cycle", "cycle", "divider", "cycle"]
    assert [r["label"] for r in l02["rows"] if r["kind"] == "cycle"] == [
        "build 1 · red → re-planned",
        "package · stopped",
        "build 2 · built",
    ]
    assert l02["status"] == "passed" and l02["attempts"] == 2


# ---- a layer's try cycles ----


def test_a_resumed_layer_has_one_row_per_try_cycle_with_dividers(tmp_path: Path) -> None:
    nodes = nodes_of(from_fixture(tmp_path, "AIR-48"), "AIR-48")
    layer = by_id(nodes, "layer-02")
    rows = layer["rows"]
    assert [r["kind"] for r in rows] == ["cycle", "divider", "cycle", "cycle", "divider", "cycle"]
    assert [r["label"] for r in rows if r["kind"] == "cycle"] == [
        "package · stopped",
        "build 1 · red → re-planned",
        "build 2 · red",
        "package · stopped",
    ]
    assert rows[1]["text"].startswith("blocked · resumed ")
    assert [r["id"] for r in rows if r["kind"] == "cycle"] == ["c1", "c2", "c3", "c4"]
    assert [j["id"] for j in rows[0]["jobs"]] == [f"c1-{j}" for j in BUILD_JOBS]
    assert [j["id"] for j in rows[-1]["jobs"]] == BUILD_JOBS and layer["jobs"] == rows[-1]["jobs"]
    assert layer["attempts"] == 2 and layer["status"] == "failed"
    assert {"text": "re-planned", "tone": "chip"} in layer["tags"]
    (l01,) = by_id(nodes, "layer-01")["rows"]  # a triage on the block names 01: it is no try of 01's
    assert l01["label"] == "build 1 · built" and l01["status"] == "passed"


def test_a_divider_says_why_the_story_stopped_and_what_was_filed(tmp_path: Path) -> None:
    rows = by_id(nodes_of(from_fixture(tmp_path, "AIR-48"), "AIR-48"), "layer-02")["rows"]
    first, second = (r for r in rows if r["kind"] == "divider")
    assert "readiness check" in texts_of(first)[0]
    assert "supervisor: package_rejected · coder" in texts_of(first)  # the triage logged for layer 01
    assert "filed AIR-49" in texts_of(second)


def test_tries_are_numbered_by_their_place_in_the_cycle() -> None:
    events = [
        {"ts": 1.0, "kind": "package_start", "layer": "01", "attempt": 1},
        {"ts": 2.0, "kind": "lint_fails", "layer": "01", "attempt": 1},
        {"ts": 3.0, "kind": "package_returned", "layer": "01", "attempt": 1, "kinds": ["lint_fails"]},
        {"ts": 4.0, "kind": "package_start", "layer": "01", "attempt": 2},
        {"ts": 5.0, "kind": "run", "stage": "proof", "studio_run": "run-p"},
        {"ts": 6.0, "kind": "amigos_proof_revised", "layer": "01"},
        {"ts": 7.0, "kind": "package_start", "layer": "01", "attempt": 2},
        {"ts": 8.0, "kind": "package_ready", "layer": "01", "attempt": 2, "studio_run": "run-a"},
    ]
    (cycle,) = cycles("01", events)
    package = cycle["jobs"][0]
    assert package["attempts"] == 3 and package["status"] == "passed"
    assert [r["text"] for r in steps_of(package)][:3] == [
        "try 1 · returned: lint_fails",
        "try 2 · proof revised",
        "try 3 · ready",
    ]
    checks = cycle["jobs"][1]
    assert checks["status"] == "passed" and checks["attempts"] == 2
    assert cycle["label"] == "package"


def test_a_package_in_its_checks_hands_the_light_to_the_checks(tmp_path: Path) -> None:
    events = [
        {"ts": 1.0, "kind": "package_start", "layer": "01", "attempt": 1},
        {"ts": 5.0, "kind": "package_checks_start", "layer": "01", "attempt": 1},
    ]
    (cycle,) = cycles("01", events)
    package, checks, *_ = cycle["jobs"]
    assert package["status"] == "passed" and checks["status"] == "running"
    assert checks["started"] == 5.0


def test_an_unknown_kind_in_a_try_is_a_finding_and_retired_mutation_events_are_not() -> None:
    events = [
        {"ts": 1.0, "kind": "package_start", "layer": "01", "attempt": 1},
        {"ts": 2.0, "kind": "vacuous_test", "layer": "01", "attempt": 1},
        {"ts": 2.5, "kind": "some_new_check", "layer": "01", "attempt": 1},
        {"ts": 2.6, "kind": "mutation_gate_skipped", "layer": "01"},
        {"ts": 3.0, "kind": "package_returned", "layer": "01", "attempt": 1, "kinds": ["vacuous_test"]},
    ]
    (cycle,) = cycles("01", events)
    kinds = [r["kind"] for r in cycle["jobs"][1]["retries"]]
    assert kinds == ["vacuous_test", "some_new_check", "package_returned"]


def test_events_for_a_layer_dropped_from_the_state_are_not_drawn(tmp_path: Path) -> None:
    layered(tmp_path, [{"ts": 1.0, "kind": "package_start", "layer": "04", "attempt": 1}])
    assert "layer-04" not in [n["id"] for n in nodes_of(tmp_path)]


# ---- a build's inner steps ----


def test_the_build_card_lists_coder_reviewer_gate_and_follow_up(tmp_path: Path) -> None:
    nodes = nodes_of(from_fixture(tmp_path, "AIR-48"), "AIR-48")
    build = next(r for r in by_id(nodes, "layer-02")["rows"] if r.get("label") == "build 2 · red")["jobs"][3]
    steps = [(r["text"], r["status"]) for r in steps_of(build)]
    # the follow-up was stopped by airlie's repeat guard (subtask_done ok: false, stop repeated_action)
    assert steps == [
        ("coder", "passed"),
        ("reviewer", "passed"),
        ("gate", "failed"),
        ("coder · gate follow-up", "failed"),
        ("gate", "failed"),
    ]
    assert build["status"] == "failed" and build["studio_run"] == "20261101-140822-8efc"
    assert "supervisor: 3 triages" in texts_of(build)


def run_story(root: Path, run: list[dict[str, Any]], *more: dict[str, Any]) -> Path:
    """Layer 01 packaged, placed and building in run `run-b`, whose log is `run`."""
    layered(
        root,
        [
            {"ts": 1.0, "kind": "package_start", "layer": "01", "attempt": 1},
            {"ts": 2.0, "kind": "package_ready", "layer": "01", "attempt": 1},
            {"ts": 3.0, "kind": "layer_start", "layer": "01", "attempt": 1, "studio_run": "run-b"},
            {"ts": 3.0, "kind": "package_placed", "layer": "01", "paths": ["tests/test_a.py"]},
            *more,
        ],
    )
    write_events(root / "runs" / "run-b" / "events.jsonl", run)
    return root


def test_a_build_is_not_done_while_its_gate_follow_up_runs(tmp_path: Path) -> None:
    run_story(
        tmp_path,
        [
            {"ts": 3.0, "kind": "subtask_start", "subtask": "build", "role": "coder"},
            {"ts": 50.0, "kind": "subtask_done", "subtask": "build", "ok": True, "fix_rounds": 0},
            {"ts": 60.0, "kind": "gate_failed", "follow_up": "build"},
            {"ts": 61.0, "kind": "follow_up", "subtask": "build", "role": "coder"},
        ],
    )
    layer = by_id(nodes_of(tmp_path), "layer-01")
    _, _, place, build, squash = layer["jobs"]
    assert place["status"] == "passed" and place["sub"] == "1 test file"
    assert build["status"] == "running" and build["live"] == "coder · gate follow-up"
    assert squash["status"] == "pending"
    assert layer["rows"][0]["label"] == "build 1"


def test_an_old_run_without_gate_events_shows_the_gate_running_after_the_coder(tmp_path: Path) -> None:
    run_story(
        tmp_path,
        [
            {"ts": 3.0, "kind": "subtask_start", "subtask": "build", "role": "coder"},
            {"ts": 50.0, "kind": "subtask_done", "subtask": "build", "ok": True, "fix_rounds": 0},
        ],
    )
    build = by_id(nodes_of(tmp_path), "layer-01")["jobs"][3]
    assert build["status"] == "running" and build["live"] == "gate"
    assert [(r["text"], r["status"]) for r in steps_of(build)] == [("coder", "passed"), ("gate", "running")]


def test_a_new_run_logs_its_gate_runs_and_a_passed_build_waits_for_its_squash(tmp_path: Path) -> None:
    run_story(
        tmp_path,
        [
            {"ts": 3.0, "kind": "subtask_start", "subtask": "build", "role": "coder"},
            {"ts": 10.0, "kind": "done_check", "subtask": "build"},
            {"ts": 12.0, "kind": "done_check_done", "subtask": "build"},
            {"ts": 20.0, "kind": "review", "subtask": "build", "verdict": "request_changes", "round": 0},
            {"ts": 21.0, "kind": "fix_round", "subtask": "build", "round": 1, "blocking": 1},
            {"ts": 30.0, "kind": "review", "subtask": "build", "verdict": "approve", "round": 1},
            {"ts": 31.0, "kind": "subtask_done", "subtask": "build", "ok": True, "fix_rounds": 1},
            {"ts": 32.0, "kind": "gate_start", "n": 1},
            {"ts": 40.0, "kind": "gate_passed", "n": 1},
            {"ts": 41.0, "kind": "run_done", "ok": True},
        ],
    )
    _, _, _, build, squash = by_id(nodes_of(tmp_path), "layer-01")["jobs"]
    assert [(r["text"], r["status"]) for r in steps_of(build)] == [
        ("coder", "passed"),
        ("done-check", "passed"),
        ("reviewer · 1 fix round", "passed"),
        ("gate", "passed"),
    ]
    assert build["status"] == "passed" and build["fix_rounds"] == 1
    assert squash["status"] == "running"


def test_supervisor_triages_go_to_the_build_they_happened_in_by_time(tmp_path: Path) -> None:
    run_story(
        tmp_path,
        [{"ts": 3.0, "kind": "subtask_start", "subtask": "build", "role": "coder"}],
        {"ts": 0.5, "kind": "supervisor_triage", "layer": "01", "trigger": "repeat_guard"},  # before it
        {"ts": 10.0, "kind": "supervisor_triage", "layer": "02", "trigger": "repeat_guard"},  # a stale layer
    )
    build = by_id(nodes_of(tmp_path), "layer-01")["jobs"][3]
    assert "supervisor: 1 triage" in texts_of(build)


# ---- publish and review ----


def test_publish_and_review_are_code_steps(tmp_path: Path) -> None:
    make_story(
        tmp_path,
        [
            {"ts": 1.0, "kind": "stage", "stage": "build"},
            {"ts": 2.0, "kind": "published", "prs": [{"branch": "b", "number": 7, "url": "u"}]},
            {"ts": 2.0, "kind": "stage", "stage": "publish"},
        ],
        stage="publish",
        layers=[],
    )
    nodes = nodes_of(tmp_path)
    publish, review = by_id(nodes, "publish"), by_id(nodes, "review")
    assert publish["lane"] == review["lane"] == "build"
    assert publish["jobs"][0]["shape"] == review["jobs"][0]["shape"] == "code"
    assert publish["status"] == "passed" and publish["jobs"][0]["sub"] == "1 PR"
    assert review["status"] == "running"
    make_story(tmp_path / "b", [{"ts": 1.0, "kind": "stage", "stage": "build"}], stage="build", layers=[])
    nodes = nodes_of(tmp_path / "b")
    assert by_id(nodes, "publish")["status"] == "running" and by_id(nodes, "review")["status"] == "pending"
