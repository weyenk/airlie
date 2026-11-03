"""The flow's now bar, exit row and liveness: a killed process draws as stopped, not running; what a story
is doing now, or why it is not; and how it left (blocked, waiting, cancelled). AIR-47's and AIR-48's real logs
(trimmed, under tests/fixtures/flow) are the cases."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.serve.flow import job_detail, story_flow
from airlie.serve.flow_now import story_live
from airlie.serve.running import ACTIVE_S
from tests.unit.test_story_flow import KEY, by_id
from tests.unit.test_story_flow_build import air47_at_16_12, from_fixture, story_files
from tests.unit.test_story_flow_plan import make_story


def last_ts(root: Path, key: str) -> float:
    return float(json.loads(story_files(root, key)[1].read_text().splitlines()[-1])["ts"])


# ---- liveness ----


def test_a_story_is_live_when_held_or_written_to_within_the_hour_unless_it_stopped() -> None:
    quiet = [{"ts": 100.0, "kind": "stage"}]
    assert story_live(quiet, None, held=False, now=100.0 + ACTIVE_S)
    assert not story_live(quiet, None, held=False, now=100.0 + ACTIVE_S + 1)
    assert story_live(quiet, 5000.0, held=False, now=5000.0 + ACTIVE_S)  # a run still writing
    assert story_live(quiet, None, held=True, now=1e9)
    for kind in ("story_done", "blocked", "waiting", "cancelled"):
        assert not story_live([{"ts": 100.0, "kind": kind}], None, held=False, now=101.0)


def test_a_story_quiet_for_over_an_hour_with_no_stop_is_stopped_not_running(tmp_path: Path) -> None:
    air47_at_16_12(tmp_path)
    flow = story_flow(tmp_path, "AIR-47", now=last_ts(tmp_path, "AIR-47") + ACTIVE_S + 1)
    assert by_id(flow["nodes"], "packages")["jobs"][0]["status"] == "stopped"
    assert by_id(flow["nodes"], "packages")["status"] == "stopped"
    assert flow["now"]["status"] == "stopped"
    assert flow["now"]["text"].startswith("Stopped at Build lane › Draft packages")
    assert "no event since" in flow["now"]["text"]
    assert flow["now"]["last_event"] is not None


def test_a_quiet_story_this_server_holds_is_still_running(tmp_path: Path) -> None:
    air47_at_16_12(tmp_path)
    flow = story_flow(tmp_path, "AIR-47", held=True, now=last_ts(tmp_path, "AIR-47") + 10 * ACTIVE_S)
    assert by_id(flow["nodes"], "packages")["jobs"][0]["status"] == "running"
    assert flow["now"]["status"] == "running"
    assert flow["now"]["path"] == ["Build lane", "Draft packages"]
    assert flow["now"]["inner"] == "L03 · package"
    assert flow["now"]["node"] == "packages" and flow["now"]["job"] == "packages"
    assert "Stopped" not in flow["now"]["text"]


def test_a_blocked_story_settles_its_running_boxes_as_failed(tmp_path: Path) -> None:
    air47_at_16_12(tmp_path)
    state, _ = story_files(tmp_path, "AIR-47")
    st = json.loads(state.read_text())
    st["blocked"] = "layer 03 broke"
    state.write_text(json.dumps(st))
    flow = story_flow(tmp_path, "AIR-47", held=True, now=last_ts(tmp_path, "AIR-47") + 1)
    assert by_id(flow["nodes"], "packages")["jobs"][0]["status"] == "failed"
    assert flow["now"]["status"] == "idle" and flow["now"]["text"].startswith("Blocked at")


# ---- the now bar when idle ----


def test_a_planned_story_waiting_for_the_build_lane_says_so(tmp_path: Path) -> None:
    events = [
        {"ts": 1.0, "kind": "story_start", "stage": "new"},
        {"ts": 2.0, "kind": "story_planned"},
        {"ts": 3.0, "kind": "story_done"},
    ]
    make_story(tmp_path, events, stage="layers")
    flow = story_flow(tmp_path, KEY, now=4.0)
    assert flow["now"] == {**flow["now"], "status": "idle", "text": "Planned · waiting for the build lane"}


def test_a_story_not_started_says_so(tmp_path: Path) -> None:
    make_story(tmp_path, [])
    assert story_flow(tmp_path, KEY, now=1.0)["now"]["text"] == "Not started"


# ---- the exit row ----


def test_a_blocked_story_has_an_exit_row_with_its_diagnosis_and_fix_card(tmp_path: Path) -> None:
    from_fixture(tmp_path, "AIR-48")
    ex = story_flow(tmp_path, "AIR-48", now=last_ts(tmp_path, "AIR-48") + 5)["exit"]
    assert ex["kind"] == "blocked" and ex["times"] == 3
    assert ex["reason"].startswith("layer 02")
    assert [j["id"] for j in ex["jobs"]] == ["diagnose", "fix", "card"]
    assert [j["shape"] for j in ex["jobs"]] == ["model", "code", "code"]
    assert ex["jobs"][1]["sub"] == "AIR-48"
    assert ex["jobs"][2]["sub"] == "→ blocked"
    assert ex["jobs"][0]["role"] == "muse"


def test_a_waiting_story_names_what_it_waits_on(tmp_path: Path) -> None:
    events = [
        {"ts": 1.0, "kind": "story_start", "stage": "new"},
        {"ts": 2.0, "kind": "waiting", "reason": "AIR-4 is not merged", "on": "AIR-4"},
    ]
    make_story(tmp_path, events)
    flow = story_flow(tmp_path, KEY, now=3.0)
    assert flow["exit"]["kind"] == "waiting" and flow["exit"]["on"] == "AIR-4"
    assert flow["exit"]["jobs"][-1]["sub"] == "→ To Do"
    assert flow["now"]["text"] == "Waiting on AIR-4"


def test_a_cancelled_story_names_the_step_it_was_in(tmp_path: Path) -> None:
    events = [
        {"ts": 1.0, "kind": "story_start", "stage": "new"},
        {"ts": 2.0, "kind": "cancelled", "stage": "layers", "during": "Layer 02"},
    ]
    make_story(tmp_path, events)
    flow = story_flow(tmp_path, KEY, now=3.0)
    assert flow["exit"]["kind"] == "cancelled"
    assert flow["now"]["text"] == "Cancelled during Layer 02"


def test_a_story_that_resumed_after_a_block_has_no_exit_row(tmp_path: Path) -> None:
    events = [
        {"ts": 1.0, "kind": "story_start", "stage": "new"},
        {"ts": 2.0, "kind": "blocked", "stage": "layers", "reason": "x"},
        {"ts": 3.0, "kind": "story_start", "stage": "layers", "resumed": True},
    ]
    make_story(tmp_path, events)
    assert story_flow(tmp_path, KEY, now=4.0)["exit"] is None


# ---- job detail ----


def test_job_detail_opens_an_earlier_cycles_build_with_its_run_trace(tmp_path: Path) -> None:
    from_fixture(tmp_path, "AIR-48")
    node = by_id(story_flow(tmp_path, "AIR-48", now=0.0)["nodes"], "layer-02")
    ids = [j["id"] for r in node["rows"] if r["kind"] == "cycle" for j in r["jobs"]]
    assert "c2-build" in ids
    d = job_detail(tmp_path, "AIR-48", "layer-02", "c2-build")
    assert d["job"]["studio_run"] and d["input_note"] != "airlie's own check, no model"


def test_job_detail_for_a_code_step_says_no_model(tmp_path: Path) -> None:
    from_fixture(tmp_path, "AIR-48")
    d = job_detail(tmp_path, "AIR-48", "layer-01", "place")
    assert d["input_note"] == "airlie's own check, no model"


def test_job_detail_opens_an_exit_job(tmp_path: Path) -> None:
    from_fixture(tmp_path, "AIR-48")
    d = job_detail(tmp_path, "AIR-48", "exit", "card")
    assert d["job"]["id"] == "card" and d["input_note"] == "airlie's own check, no model"


def test_job_detail_keeps_the_meetings_note(tmp_path: Path) -> None:
    from_fixture(tmp_path, "AIR-48")
    d = job_detail(tmp_path, "AIR-48", "amigos", "meeting")
    assert d["input_note"].startswith("a meeting of product")
