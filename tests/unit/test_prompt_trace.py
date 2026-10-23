"""A role's opening message is kept in the run's trace, so what a tester, coder or architect was told can be
read afterwards (2026-10-23: the step traces start at the first tool call; the prompt that led to it was
nowhere)."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.engine import FakeEngine
from airlie.router import ChiefRouter
from airlie.topology.studio import StudioOrchestrator
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_studio_orchestrator import (
    APPROVE,
    CHANGES,
    DONE,
    PLAN,
    RUN,
    SYNTH,
    WRITE_PASS,
    cfg_for,
)


async def test_each_subtask_keeps_the_messages_its_conversation_opened_with(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {"chief": [PLAN, SYNTH], "coder": [WRITE_PASS, RUN, DONE], "docs": [DONE], "reviewer": [APPROVE]}
    )
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    saved = json.loads((report.run_dir.path / "trace" / "fix" / "prompt.json").read_text())
    sent = [r for r in engine.requests if r.role == "coder"][0].messages
    assert saved["role"] == "coder" and saved["attempt"] == 0
    assert [m["role"] for m in saved["messages"]] == ["system", "user"]
    assert saved["messages"][0]["content"] == sent[0].content
    assert saved["messages"][1]["content"] == sent[1].content
    assert "make the check pass" in saved["messages"][1]["content"]  # the subtask's brief
    assert (report.run_dir.path / "trace" / "note" / "prompt.json").exists()


async def test_a_fix_round_keeps_its_own_opening_message(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {
            "chief": [PLAN, SYNTH],
            "coder": [WRITE_PASS, RUN, DONE, WRITE_PASS, DONE],
            "docs": [DONE],
            "reviewer": [CHANGES, APPROVE],
        }
    )
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    trace = report.run_dir.path / "trace" / "fix"
    first = json.loads((trace / "prompt.json").read_text())
    fix = json.loads((trace / "round-1" / "prompt.json").read_text())
    assert first["attempt"] == 0 and fix["attempt"] == 1
    # a continuation resumes the conversation: only the message it added is its opening
    assert [m["role"] for m in fix["messages"]] == ["user"]
    assert fix["messages"][0]["content"].startswith("A reviewer found blocking problems")
