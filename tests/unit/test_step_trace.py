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
