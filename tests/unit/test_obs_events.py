"""Step events: the JSONL line splits prefill from generation and carries the slice keys.

The metrics tab (story: Metrics) reads these lines back through airmetrics; the keys it groups by
(model, role, topology, story) and the timing it charts (prefill_ms, gen_ms) must be on the line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from airlie.engine.types import ToolCall
from airlie.obs import EventSink, RunDir
from airlie.orchestrator import LoggingObserver
from airlie.teams.protocol import StepRecord


def _step_record() -> StepRecord:
    """A refine-shaped step as NativeTeam records it from a fake GenResult: 800 ms of the 1300 ms
    request was prefill (ttft), so generation is the remaining 500 ms; ~20k prompt tokens re-read to
    write ~30 completion tokens."""
    return StepRecord(
        team_id="team-1",
        step_num=3,
        u_t=ToolCall("call_1", "write_file", '{"path": "x.py"}'),
        o_t="wrote x.py",
        ok=True,
        assistant_text="I wrote the file.",
        prompt_tokens=20_000,
        completion_tokens=30,
        gen_ms=500.0,
        prefill_ms=800.0,
        tool_ms=12.5,
    )


def _last_step_line(events_path: Path) -> dict[str, object]:
    lines = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    assert lines, "no events were written"
    step_lines = [line for line in lines if line["kind"] == "step"]
    assert step_lines, "no step event was written"
    return step_lines[-1]


@pytest.mark.asyncio
async def test_step_event_splits_prefill_from_generation(tmp_path: Path) -> None:
    run_dir = RunDir.create(tmp_path, "run-split")
    sink = EventSink(run_dir.events_path, run_dir.run_id)
    observer = LoggingObserver(sink, run_dir)
    await observer.after_step(_step_record())
    line = _last_step_line(run_dir.events_path)
    assert line["prefill_ms"] == 800.0
    assert line["gen_ms"] == 500.0
    # generation-only: a refine step re-reads ~20k prompt tokens to write ~30, so gen < prefill
    assert line["gen_ms"] < line["prefill_ms"]
    assert line["prompt_tokens"] == 20_000
    assert line["completion_tokens"] == 30


@pytest.mark.asyncio
async def test_step_event_carries_slice_keys(tmp_path: Path) -> None:
    run_dir = RunDir.create(tmp_path, "run-keys")
    sink = EventSink(run_dir.events_path, run_dir.run_id)
    observer = LoggingObserver(sink, run_dir, model="qwen3-coder", role_name="coder", topology="teams")
    await observer.after_step(_step_record())
    line = _last_step_line(run_dir.events_path)
    assert line["model"] == "qwen3-coder"
    assert line["role"] == "coder"
    assert line["topology"] == "teams"
    assert line["story"] is None  # a task run lives under runs/, not stories/<key>/
    assert line["span_id"]
    assert line["parent_id"]


@pytest.mark.asyncio
async def test_step_event_span_ids_per_step_and_run(tmp_path: Path) -> None:
    run_dir = RunDir.create(tmp_path, "run-spans")
    sink = EventSink(run_dir.events_path, run_dir.run_id)
    observer = LoggingObserver(sink, run_dir)
    await observer.after_step(_step_record())
    step2 = _step_record()
    step2.step_num = 4
    await observer.after_step(step2)
    lines = [json.loads(line) for line in run_dir.events_path.read_text().splitlines() if line.strip()]
    step_lines = [line for line in lines if line["kind"] == "step"]
    assert len(step_lines) == 2
    assert step_lines[0]["span_id"] != step_lines[1]["span_id"]  # one span per step
    assert step_lines[0]["parent_id"] == step_lines[1]["parent_id"]  # one run-level span per run


@pytest.mark.asyncio
async def test_step_event_story_key_for_story_run(tmp_path: Path) -> None:
    # a story run lives under state_dir/stories/<key>/runs/<run_id>/
    run_dir = RunDir.create_for_story(tmp_path, "AIR-12", "run-story")
    sink = EventSink(run_dir.events_path, run_dir.run_id)
    observer = LoggingObserver(sink, run_dir)
    await observer.after_step(_step_record())
    line = _last_step_line(run_dir.events_path)
    assert line["story"] == "AIR-12"


@pytest.mark.asyncio
async def test_run_dir_create_for_story_lives_under_stories(tmp_path: Path) -> None:
    run_dir = RunDir.create_for_story(tmp_path, "AIR-12", "run-story")
    assert run_dir.path == tmp_path / "stories" / "AIR-12" / "runs" / "run-story"
    assert run_dir.run_id == "run-story"
    assert (run_dir.path / "trace").is_dir()
