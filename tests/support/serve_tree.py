"""Shared helpers extracted from tests/unit/test_serve_tree.py for tests.support.serve_tree."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RUN = "20261023-120000-abcd"


def ev(kind: str, ts: float, **fields: Any) -> dict[str, Any]:
    return {"ts": ts, "run_id": RUN, "kind": kind, **fields}


def step(
    team: str, n: int, ts: float, *, tool: str = "read_file", ok: bool = True, role: str = "architect"
) -> dict:
    return ev(
        "step",
        ts,
        team_id=team,
        step=n,
        tool=tool,
        ok=ok,
        prompt_tokens=1000,
        completion_tokens=100,
        model="qwen-architect",
        role=role,
        topology="studio",
    )


def handoff(src: str, dst: str, what: str, text: str, ts: float, **more: Any) -> dict[str, Any]:
    return ev("handoff", ts, src=src, dst=dst, what=what, text=text, **more)


STUDIO_START = [
    ev("run_start", 1.0, goal="fix the build", k=1, repo="/r", orchestrator="studio"),
    ev("plan", 2.0, assumptions=[], subtasks=["fix", "note"], roles=["architect", "docs"], pinned=False),
    ev("route", 2.1, subtask="fix", chosen="architect", decision="hint", confidence=0.9),
    ev("skill", 2.2, subtask="fix", chosen="python", decision="pick", confidence=0.8),
    handoff("chief", "fix", "brief", "make the check pass", 2.3),
    ev("subtask_start", 2.3, subtask="fix", role="architect", skill="python", writes=True),
    step("fix", 1, 3.0),
    step("fix", 2, 4.0, tool="write_file"),
]

STUDIO_DONE = [
    *STUDIO_START,
    step("fix", 3, 5.0, tool="run_tests"),
    handoff("fix", "reviewer", "diff", "1 file changed: PASS", 6.0, role="reviewer"),
    ev("review", 7.0, subtask="fix", verdict="request_changes", findings=1, blocking=1, round=0),
    handoff("reviewer", "fix", "findings", "request_changes: PASS: wrong content", 7.0, role="reviewer"),
    ev("fix_round", 7.1, subtask="fix", round=1, blocking=1),
    step("fix", 1, 8.0, tool="write_file"),
    handoff("fix", "reviewer", "diff", "1 file changed: PASS", 9.0, role="reviewer"),
    ev("review", 10.0, subtask="fix", verdict="approve", findings=0, blocking=0, round=1),
    handoff("reviewer", "fix", "findings", "approve: no findings", 10.0, role="reviewer"),
    ev(
        "subtask_done",
        10.1,
        subtask="fix",
        role="architect",
        ok=True,
        stop_reason="done",
        steps=4,
        fix_rounds=1,
    ),
    handoff("fix", "chief", "result", "wrote PASS; the check passes", 10.1),
    ev("route", 10.2, subtask="note", chosen="docs", decision="hint", confidence=0.9),
    handoff("chief", "note", "brief", "describe the repo", 10.3),
    handoff("fix", "note", "result", "wrote PASS; the check passes", 10.3),
    ev("subtask_start", 10.3, subtask="note", role="docs", skill=None, writes=False),
    step("note", 1, 11.0, role="docs"),
    ev("subtask_done", 12.0, subtask="note", role="docs", ok=True, stop_reason="done", steps=1, fix_rounds=0),
    handoff("note", "chief", "result", "the repo holds a check", 12.0),
    ev("synthesis", 13.0, chars=8, open_questions=[]),
    handoff("chief", "synthesis", "answer", "all done", 13.0),
    ev("run_done", 14.0, ok=True, wall_ms=13000.0, subtasks=2, tests=True),
]

FORK = [
    ev("run_start", 1.0, goal="g", topology="fork", repo="/r", width=2),
    ev("branch_start", 1.1, branch="b0", worktree="/w/b0", child=False),
    step("b0", 1, 2.0),
    ev("branch_done", 3.0, branch="b0", ok=False, stop_reason="stuck", steps=1, prompt_tokens=1000),
    ev(
        "fork_point",
        3.1,
        round=1,
        parent="b0",
        reason="stuck",
        children=["b1", "b2"],
        hypotheses=["the parser is wrong", "the test is wrong"],
    ),
    ev("branch_start", 3.2, branch="b1", worktree="/w/b1", child=True),
    ev("branch_start", 3.2, branch="b2", worktree="/w/b2", child=True),
    step("b1", 1, 4.0, tool="write_file"),
]


def write_run(state: Path, run: str, events: list[dict[str, Any]]) -> None:
    d = state / "runs" / run
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
