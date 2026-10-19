"""The step trace a LoggingObserver writes keeps what the model thought, so a run whose steps are long can be
read afterwards (AIR-28, 2026-10-18: seven steps ran their thinking to the cap; nothing said about what)."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.engine.types import ToolCall
from airlie.obs.events import EventSink
from airlie.obs.run_dir import RunDir
from airlie.orchestrator import LoggingObserver
from airlie.teams import StepRecord


async def test_the_step_trace_keeps_the_reasoning(tmp_path: Path) -> None:
    run_dir = RunDir.create(tmp_path, "r1")
    obs = LoggingObserver(EventSink(run_dir.events_path), run_dir)
    step = StepRecord(
        team_id="build",
        step_num=3,
        u_t=ToolCall(id="c1", name="read_file", arguments='{"path": "a.py"}'),
        o_t="1: x = 1",
        ok=True,
        assistant_text="Let me check one more thing.",
        reasoning="I have what I need, but a.py might import something I missed.",
    )
    await obs.after_step(step)
    trace = json.loads((run_dir.path / "trace" / "build" / "step-003.json").read_text())
    assert trace["reasoning"] == "I have what I need, but a.py might import something I missed."
    assert trace["assistant_text"] == "Let me check one more thing."
    event = json.loads(run_dir.events_path.read_text().splitlines()[-1])
    assert "reasoning" not in event  # the thinking is for the trace, not the event log


async def test_a_later_attempt_of_a_team_keeps_its_own_step_trace(tmp_path: Path) -> None:
    """A fix round counts its steps from 1 again: its traces went over the build run's (AIR-7 layer 02,
    2026-10-18: steps 1-14 of the first run were gone). A later attempt files under round-N, as its review."""
    run_dir = RunDir.create(tmp_path, "r1")
    obs = LoggingObserver(EventSink(run_dir.events_path), run_dir)
    call = ToolCall(id="c1", name="read_file", arguments='{"path": "a.py"}')
    await obs.after_step(StepRecord(team_id="build", step_num=1, u_t=call, o_t="first", ok=True))
    await obs.after_step(StepRecord(team_id="build", step_num=1, u_t=call, o_t="fix", ok=True, attempt=1))
    trace = run_dir.path / "trace" / "build"
    assert json.loads((trace / "step-001.json").read_text())["o_t"] == "first"
    assert json.loads((trace / "round-1" / "step-001.json").read_text())["o_t"] == "fix"
