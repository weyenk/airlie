"""The golden layer build (generic nodes Part 2, Task 1): one layer driven through every phase of the
studio's build (a done-check miss, a review round with a blocking finding and its fix, a red gate with the
supervisor's advice, a follow-up and a green regate), and what it left behind: the story's events, the
studio run's events, the supervisor's ledger rows, the saved state and the continuation prompts.

The `GOLDEN_*` constants at the end were recorded from the base commit's driver and pasted in (by running
`golden_layer_events` and its siblings there); no test computes them. Tasks 3 and 5 run the same script
through the split studio and the composer and compare with them. Only what differs run to run is
neutralised: timestamps, ids, timings, paths, commit shas, and the colour codes and clock of pytest's output
in a gate's tail."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from airlie.bench.trace_diff import TIMING, VOLATILE, same
from airlie.engine import FakeEngine, FakeReply
from airlie.story.board import text_card
from airlie.supervisor.ledger import Row
from airlie.topology.studio import StudioOrchestrator, SubtaskRun
from airlie.topology.studio_plan import Subtask
from tests.support.story import STORY, kinds_all, neutral, pipeline
from tests.support.story_layer_checks_flow import (
    BROKEN,
    FEATURE,
    FIXED,
    LAYERS,
    PYTEST,
    RED,
    calc_repo,
    write,
)
from tests.support.studio import APPROVE, CHANGES, DONE
from tests.support.supervisor_flow import SUPERVISOR_ROLES, answer, rows

SCRIPT_LAYER: dict[str, list[FakeReply]] = {
    "product": [STORY],
    "architect": [LAYERS, RED],
    "coder": [
        FEATURE,  # the coder turn: mul, as the package asks
        DONE,
        write("README.md", "mul multiplies.\n"),  # the done-check finds a miss and fixes it
        DONE,
        BROKEN,  # the fix round for the blocking finding breaks add: the gate goes red
        DONE,
        FIXED,  # the follow-up carries the gate's output and the advice
        DONE,
    ],
    "reviewer": [CHANGES, APPROVE],  # round 0 blocks, round 1 approves the fix
    "supervisor": [answer("Put add back as it was.")],
}

SHA = "0" * 40  # the shas are spelled <sha> by `same`; `neutral` wants one to replace


def layer_engine() -> FakeEngine:
    """A fresh engine on SCRIPT_LAYER (an engine's script is consumed as it is read)."""
    return FakeEngine({role: list(replies) for role, replies in SCRIPT_LAYER.items()})


def clean(
    items: list[dict[str, Any]], tmp_path: Path, *, drop: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Events without their volatile fields (and `drop`), their paths and shas spelled the same."""
    kept = [{k: v for k, v in e.items() if k not in VOLATILE | drop} for e in items]
    out: list[dict[str, Any]] = json.loads(same(json.dumps(neutral(json.dumps(kept), tmp_path, SHA))))
    return out


def story_dir(tmp_path: Path) -> Path:
    [path] = (p for p in (tmp_path / "state" / "stories").iterdir() if p.is_dir())
    return path


def build_run(tmp_path: Path) -> Path:
    """The studio run that built the layer (the one `layer_start` names)."""
    [start] = [e for e in kinds_all(story_dir(tmp_path) / "events.jsonl") if e["kind"] == "layer_start"]
    return tmp_path / "state" / "runs" / start["studio_run"]


async def golden_layer_events(tmp_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Row]]:
    """Build the scripted layer with the driver (composer off, supervisor on, done-check on) and return the
    story's events, the build run's events and the ledger's rows, neutralised. The story's directory,
    the run's trace and the ledger stay under `tmp_path` for `golden_state` and `golden_prompts`."""
    repo = calc_repo(tmp_path)
    pipe = pipeline(
        tmp_path,
        repo,
        layer_engine(),
        test_cmd=PYTEST,
        story={"supervisor": {"enabled": True}},
        roles=SUPERVISOR_ROLES,
        done_check=True,
        composer=False,
    )
    st = await pipe.run(text_card("multiply"))
    assert st.blocked is None and list(st.built) == ["01"], st.blocked
    story = clean(kinds_all(story_dir(tmp_path) / "events.jsonl"), tmp_path)
    studio = clean(kinds_all(build_run(tmp_path) / "events.jsonl"), tmp_path, drop=TIMING)
    ledger = [
        replace(r, id=0, at=0.0, seconds=0.0, story="<story>", studio_run="<run>") for r in rows(tmp_path)
    ]
    return story, studio, ledger


def golden_state(tmp_path: Path) -> dict[str, Any]:
    """The saved state of the golden story, reduced to what the build decides: the stage, no open attempt, no
    block, the layer's `built` record (its keys, its subtasks, its tests and branch) and how many runs the
    story named."""
    saved = json.loads(same((story_dir(tmp_path) / "state.json").read_text()))
    built = saved["built"]["01"]
    return {
        "stage": saved["stage"],
        "attempt": saved["attempt"],
        "blocked": saved["blocked"],
        "built_layers": list(saved["built"]),
        "built_keys": list(built),
        "branch": built["branch"],
        "subtasks": built["subtasks"],
        "tests": built["tests"],
        "runs": len(saved["runs"]),
    }


def golden_prompts(tmp_path: Path) -> dict[str, str]:
    """The last message of every prompt the coder was sent in the build run, by its trace folder: its brief
    (`build`), the done-check (`build/round-1`), the fix round (`round-2`) and the follow-up (`round-3`)."""
    trace = build_run(tmp_path) / "trace"
    found = {}
    for f in sorted(trace.rglob("prompt.json")):
        messages = json.loads(f.read_text())["messages"]
        found[str(f.parent.relative_to(trace))] = same(messages[-1]["content"])
    return found


class AdvisedThenDies(StudioOrchestrator):
    """A studio whose coder turn asks the supervisor (a ledger row opens, waiting for its outcome) and then
    raises past the turn's own catch, as a cancel or a bug outside the team does."""

    async def coder_turn(self, subtask: Subtask) -> SubtaskRun:
        assert self.advisor is not None
        await self.advisor.advise("gate_retry", subtask.id, "coder", "the gate is red", kinds=("gate_fails",))
        raise RuntimeError("the engine fell over")


# ---- recorded on 1de74237 (the driver, composer off) ----

GOLDEN_STORY_KINDS = [
    "story_start",
    "stage",
    "run",
    "stage",
    "run",
    "stage",
    "packages_drafting",
    "run",
    "package_draft_start",
    "package_drafted",
    "package_start",
    "package_checks_start",
    "package_ready",
    "layer_start",
    "brief",
    "package_placed",
    "supervisor_triage",
    "layer_built",
    "stage",
    "published",
    "stage",
    "stage",
    "story_done",
]

GOLDEN_STUDIO_KINDS = [
    "run_start",
    "locked",
    "plan",
    "route",
    "handoff",
    "subtask_start",
    "step",
    "reply",
    "steer",
    "done_check",
    "step",
    "reply",
    "done_check_done",
    "handoff",
    "review",
    "handoff",
    "fix_round",
    "step",
    "reply",
    "handoff",
    "review",
    "handoff",
    "subtask_done",
    "handoff",
    "gate_start",
    "gate_failed",
    "steer",
    "follow_up",
    "step",
    "reply",
    "subtask_done",
    "handoff",
    "gate_start",
    "gate_passed",
    "run_done",
]

GOLDEN_STORY_LAYER = [
    {
        "kind": "layer_start",
        "layer": "01",
        "branch": "add-avatar-upload-01-mul",
        "base": "origin/master",
        "base_sha": "<sha>",
        "attempt": 1,
    },
    {"kind": "package_placed", "layer": "01", "paths": ["tests/test_calc.py"]},
    {
        "kind": "supervisor_triage",
        "trigger": "gate_retry",
        "category": "missing_code",
        "owner": "coder",
        "ledger_id": 1,
        "layer": "01",
    },
    {"kind": "layer_built", "layer": "01", "branch": "add-avatar-upload-01-mul", "sha": "<sha>"},
]

GOLDEN_STUDIO = [
    {
        "kind": "run_start",
        "goal": "Add avatar upload: layer 1/1 Multiply",
        "k": 1,
        "repo": "<root>/repo",
        "orchestrator": "studio",
        "given_plan": True,
    },
    {
        "kind": "locked",
        "owner": "architect",
        "paths": ["tests/test_calc.py"],
        "tests": {"tests/test_calc.py": ["test_mul"]},
    },
    {"kind": "plan", "assumptions": [], "subtasks": ["build"], "roles": ["coder"], "pinned": True},
    {
        "kind": "route",
        "subtask": "build",
        "chosen": "coder",
        "decision": "pinned",
        "confidence": 1.0,
        "probabilities": {"coder": 1.0},
        "hint": "coder",
        "criteria": ["architect", "coder", "docs", "product", "qa"],
    },
    {
        "kind": "handoff",
        "src": "chief",
        "dst": "build",
        "what": "brief",
        "text": (
            "Story: Add avatar upload. Users upload an avatar and see it.\n"
            "This is layer 1 of 1 of a stacked change: Multiply.\n"
            "Scope of this layer: calc.py\n"
            "Later layers (not yours; leave their work alone): none\n"
            "\n"
            "## The tests you make pass (in the worktree, locked)\n"
            "The acceptance criteria this "
        ),
    },
    {"kind": "subtask_start", "subtask": "build", "role": "coder", "skill": None, "writes": True},
    {
        "kind": "step",
        "team_id": "build",
        "step": 1,
        "tool": "write_file",
        "ok": True,
        "reasoning_chars": 0,
        "answer_chars": 122,
        "model": "base",
        "role": "coder",
        "topology": "studio",
        "story": None,
    },
    {
        "kind": "reply",
        "team_id": "build",
        "role": "coder",
        "model": "base",
        "reasoning_chars": 0,
        "answer_chars": 4,
    },
    {"kind": "steer", "id": "done-check", "role": "coder", "subtask": "build"},
    {"kind": "done_check", "subtask": "build", "role": "coder", "fresh": False},
    {
        "kind": "step",
        "team_id": "build",
        "step": 1,
        "tool": "write_file",
        "ok": True,
        "reasoning_chars": 0,
        "answer_chars": 64,
        "model": "base",
        "role": "coder",
        "topology": "studio",
        "story": None,
    },
    {
        "kind": "reply",
        "team_id": "build",
        "role": "coder",
        "model": "base",
        "reasoning_chars": 0,
        "answer_chars": 4,
    },
    {"kind": "done_check_done", "subtask": "build", "stop_reason": "done", "steps": 1, "changed": True},
    {
        "kind": "handoff",
        "src": "build",
        "dst": "reviewer",
        "what": "diff",
        "text": "2 files changed: README.md, calc.py",
        "role": "reviewer",
    },
    {
        "kind": "review",
        "subtask": "build",
        "verdict": "request_changes",
        "findings": 1,
        "blocking": 1,
        "round": 0,
    },
    {
        "kind": "handoff",
        "src": "reviewer",
        "dst": "build",
        "what": "findings",
        "text": "request_changes: PASS:1 wrong content",
        "role": "reviewer",
    },
    {"kind": "fix_round", "subtask": "build", "round": 1, "blocking": 1},
    {
        "kind": "step",
        "team_id": "build",
        "step": 1,
        "tool": "write_file",
        "ok": True,
        "reasoning_chars": 0,
        "answer_chars": 122,
        "model": "base",
        "role": "coder",
        "topology": "studio",
        "story": None,
    },
    {
        "kind": "reply",
        "team_id": "build",
        "role": "coder",
        "model": "base",
        "reasoning_chars": 0,
        "answer_chars": 4,
    },
    {
        "kind": "handoff",
        "src": "build",
        "dst": "reviewer",
        "what": "diff",
        "text": "2 files changed: README.md, calc.py",
        "role": "reviewer",
    },
    {"kind": "review", "subtask": "build", "verdict": "approve", "findings": 0, "blocking": 0, "round": 1},
    {
        "kind": "handoff",
        "src": "reviewer",
        "dst": "build",
        "what": "findings",
        "text": "approve: no findings",
        "role": "reviewer",
    },
    {
        "kind": "subtask_done",
        "subtask": "build",
        "role": "coder",
        "ok": True,
        "stop_reason": "done",
        "steps": 1,
        "fix_rounds": 1,
    },
    {"kind": "handoff", "src": "build", "dst": "chief", "what": "result", "text": "done"},
    {"kind": "gate_start", "n": 1},
    {
        "kind": "gate_failed",
        "follow_up": "build",
        "output": (
            "F.                                                                       [100%]\n"
            "=================================== FAILURES ==================================="
            "\n"
            "___________________________________ test_add ___________________________________"
            "\n"
            "\n"
            "    def test_add():\n"
            ">       assert add(1, 2) == 3\n"
            "E       assert -1 == 3\n"
            "E        +  where -1 = add(1, 2)\n"
            "\n"
            "tests/test_base.py:5: AssertionError\n"
            "=========================== short test summary info ============================"
            "\n"
            "FAILED tests/test_base.py::test_add - assert -1 == 3\n"
            "1 failed, 1 passed in <s>\n"
            "\n"
            "[exit 1]"
        ),
    },
    {"kind": "steer", "id": "gate-failed", "role": "coder", "subtask": "build"},
    {"kind": "follow_up", "subtask": "build", "role": "coder"},
    {
        "kind": "step",
        "team_id": "build",
        "step": 1,
        "tool": "write_file",
        "ok": True,
        "reasoning_chars": 0,
        "answer_chars": 154,
        "model": "base",
        "role": "coder",
        "topology": "studio",
        "story": None,
    },
    {
        "kind": "reply",
        "team_id": "build",
        "role": "coder",
        "model": "base",
        "reasoning_chars": 0,
        "answer_chars": 4,
    },
    {
        "kind": "subtask_done",
        "subtask": "build",
        "role": "coder",
        "ok": True,
        "stop_reason": "done",
        "steps": 1,
        "fix_rounds": 1,
    },
    {"kind": "handoff", "src": "build", "dst": "chief", "what": "result", "text": "done"},
    {"kind": "gate_start", "n": 2},
    {"kind": "gate_passed", "n": 2},
    {"kind": "run_done", "ok": True, "subtasks": 1, "tests": True},
]

GOLDEN_LEDGER = [
    (
        "gate_retry",
        "build",
        "coder",
        ("gate_fails",),
        "missing_code",
        "coder",
        "Put add back as it was.",
        "helped",
        "01",
    )
]

GOLDEN_STATE = {
    "stage": "review",
    "attempt": None,
    "blocked": None,
    "built_layers": ["01"],
    "built_keys": ["branch", "sha", "run_id", "subtasks", "gate", "package", "tests"],
    "branch": "add-avatar-upload-01-mul",
    "subtasks": [
        {"id": "build", "role": "coder", "stop": "done", "review": "approve", "fix_rounds": 1, "findings": []}
    ],
    "tests": ["tests/test_calc.py::test_mul"],
    "runs": 4,
}

GOLDEN_NUDGES = {
    "build": (
        "Goal:\n"
        "Story: Add avatar upload. Users upload an avatar and see it.\n"
        "This is layer 1 of 1 of a stacked change: Multiply.\n"
        "Scope of this layer: calc.py\n"
        "Later layers (not yours; leave their work alone): none\n"
        "\n"
        "## The tests you make pass (in the worktree, locked)\n"
        "The acceptance criteria this layer makes true, each with the tests that prove it"
        ":\n"
        "- **AC1** Given a user, when they upload a PNG, then it is stored.\n"
        "  - tests/test_calc.py::test_mul\n"
        "- **AC2** Given a stored avatar, when the profile loads, then it is shown.\n"
        "  - tests/test_calc.py::test_mul\n"
        "\n"
        "## Files\n"
        "- calc.py (modify)\n"
        "\n"
        "## Interfaces\n"
        "- calc.py: def mul(a, b) -> int\n"
        "  the product\n"
        "\n"
        "## Steps\n"
        "1. add mul(a, b) to calc.py returning a * b\n"
        "\n"
        "## Done when\n"
        "run_tests passes\n"
        "\n"
        "## Files given for you\n"
        "They are as they are at this layer's base, and the locked tests as they are in y"
        "our worktree. Whole files are complete: do not read them again. Outlines and spa"
        "ns give the line to read from; read a file listed as not shown only when you nee"
        "d it.\n"
        "\n"
        "### tests/test_calc.py (whole, 5 lines)\n"
        "```\n"
        "from calc import mul\n"
        "\n"
        "\n"
        "def test_mul():\n"
        "    assert mul(2, 3) == 6\n"
        "\n"
        "```\n"
        "\n"
        "### calc.py (whole, 2 lines)\n"
        "```\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "```\n"
        "\n"
        "The tests for this layer were written before you by the architect and are in the"
        " worktree; airlie has checked that each fails without the code, but for a test m"
        "arked a regression guard, which pins what already holds and must keep passing. T"
        "hey are the contract, and you cannot change them: follow the steps and make them"
        " pass. What else is in their files was there before (fakes, helpers, imports): c"
        "hange that where your change needs it. If a test is wrong about a name or a sign"
        "ature the steps fix, say which and why when you stop. Add tests in a new file fo"
        "r anything else you change. Your run_tests runs this layer's tests; once they pa"
        "ss, you are done: airlie runs the whole suite after review.\n"
        "\n"
        "`run_tests` runs the repository's whole gate with pytest limited to your test fi"
        "les. Before it runs, `ruff format` and ruff's fixable lint are applied for you: "
        "do not spend steps on formatting or on a lint ruff can fix. Read what it still r"
        "eports.\n"
        "\n"
        "Follow AGENTS.md at the repository root, if it has one.\n"
        "\n"
        "Change an existing file with edit_file, not by rewriting it.\n"
        "\n"
        "Stay inside this layer's scope. Run run_tests until the layer's tests pass, then"
        " stop with two or three sentences on what you changed.\n"
        "\n"
        "You are build. You are working inside a git checkout of the repository (studio)."
        " Use the tools to inspect and change it, run the tests, and stop with a short su"
        "mmary when they pass."
    ),
    "build/round-1": (
        "Before review: check your change against the brief's acceptance criteria and its"
        " exact wording. For each criterion, quote the lines of the diff below that make "
        "it true, or say 'not met'. Where the brief names a value, a message, a field, or"
        " which value to compare, check the diff uses exactly that. Anything not met or n"
        "ot exact: fix it now, run run_tests, then stop. If everything is met, reply with"
        " the checklist and stop.\n"
        "\n"
        "## Your diff\n"
        "```diff\n"
        "diff --git a/calc.py b/calc.py\n"
        "index 4693ad3..717f7ed 100644\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,6 @@\n"
        " def add(a, b):\n"
        "     return a + b\n"
        "+\n"
        "+\n"
        "+def mul(a, b):\n"
        "+    return a * b\n"
        "\n"
        "```"
    ),
    "build/round-2": (
        "A reviewer found blocking problems with your change. Fix each one, run the tests"
        ", then stop:\n"
        "- PASS:1: wrong content (evidence: x)"
    ),
    "build/round-3": (
        "Your tests pass, but the whole suite fails with your change: the output is below"
        ". Fix the code so the whole suite passes; the acceptance tests stay as they are."
        " Run run_tests, then stop.\n"
        "\n"
        "```\n"
        "F.                                                                       [100%]\n"
        "=================================== FAILURES ==================================="
        "\n"
        "___________________________________ test_add ___________________________________"
        "\n"
        "\n"
        "    def test_add():\n"
        ">       assert add(1, 2) == 3\n"
        "E       assert -1 == 3\n"
        "E        +  where -1 = add(1, 2)\n"
        "\n"
        "tests/test_base.py:5: AssertionError\n"
        "=========================== short test summary info ============================"
        "\n"
        "FAILED tests/test_base.py::test_add - assert -1 == 3\n"
        "1 failed, 1 passed in <s>\n"
        "\n"
        "[exit 1]\n"
        "```\n"
        "\n"
        "Supervisor: a second model read your work and the check's output. Its diagnosis "
        "and your next step:\n"
        "Put add back as it was."
    ),
}
