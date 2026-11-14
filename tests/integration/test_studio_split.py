"""The golden layer build (generic nodes Part 2, Task 1): a characterization of the studio's layer build as
the base commit's driver runs it, recorded before the studio is split into steps. It is green on the base
commit by design and every later task keeps it green: the same events, state, prompts and ledger rows, so
a refactor cannot change what a layer build does without this test saying so. The script (SCRIPT_LAYER)
drives the layer through every phase, and the first test pins that it still does: a refactor that skipped
a phase would otherwise pass by recording less."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from airlie.engine import FakeEngine
from airlie.report import RunReport
from airlie.router import ChiefRouter
from airlie.story.briefs import layer_plan
from airlie.topology.studio import StudioOrchestrator, WrongTest
from airlie.topology.studio_plan import Plan, Subtask
from tests.support.git import repo_with_marker_tests
from tests.support.layer_golden import (
    GOLDEN_LEDGER,
    GOLDEN_NUDGES,
    GOLDEN_STATE,
    GOLDEN_STORY_KINDS,
    GOLDEN_STORY_LAYER,
    GOLDEN_STUDIO,
    GOLDEN_STUDIO_KINDS,
    SCRIPT_LAYER,
    TIMING,
    clean,
    golden_layer_events,
    golden_prompts,
    golden_state,
    same,
)
from tests.support.story import kinds_all
from tests.support.story_layer_checks_flow import PYTEST, calc_repo
from tests.support.studio import APPROVE, CHANGES, DONE, PLAN, RUN, SYNTH, WRITE_PASS, cfg_for


async def test_the_golden_script_exercises_every_phase_of_a_layer_build(tmp_path: Path) -> None:
    story, studio, ledger = await golden_layer_events(tmp_path)
    seen = [e["kind"] for e in studio]

    def one(kind: str, **fields: object) -> dict[str, object]:
        [found] = [e for e in studio if e["kind"] == kind and all(e.get(k) == v for k, v in fields.items())]
        return found

    # the coder turn, then a done-check that found a miss and changed the tree
    assert seen.index("subtask_start") < seen.index("done_check") < seen.index("done_check_done")
    assert one("done_check_done")["changed"] is True
    # one review round with a blocking finding, a fix, and the approving round after it
    blocked = one("review", round=0)
    assert (blocked["verdict"], blocked["blocking"]) == ("request_changes", 1)
    assert one("fix_round")["round"] == 1
    assert one("review", round=1)["verdict"] == "approve"
    assert seen.index("review") < seen.index("fix_round") < seen.index("gate_start")
    # a red gate with advice, a follow-up, a green regate
    gate_starts = [e for e in studio if e["kind"] == "gate_start"]
    assert [e["n"] for e in gate_starts] == [1, 2]
    assert one("gate_failed")["follow_up"] == "build"
    assert seen.index("gate_failed") < seen.index("follow_up") < seen.index("gate_passed")
    assert one("gate_passed")["n"] == 2 and one("run_done")["tests"] is True
    assert [(r.trigger, r.outcome) for r in ledger] == [("gate_retry", "helped")]  # the advice
    assert "supervisor_triage" in [e["kind"] for e in story] and "layer_built" in [e["kind"] for e in story]
    assert one("steer", id="gate-failed")["role"] == "architect"
    assert "Put add back as it was." in golden_prompts(tmp_path)["build/round-3"]  # the follow-up carries it


async def test_a_layer_build_emits_the_recorded_events_state_and_ledger_rows(tmp_path: Path) -> None:
    story, studio, ledger = await golden_layer_events(tmp_path)
    assert [e["kind"] for e in story] == GOLDEN_STORY_KINDS
    assert [e["kind"] for e in studio] == GOLDEN_STUDIO_KINDS
    # the fields of the layer's own events, not only their order
    layer = [
        e for e in story if e["kind"] in {"layer_start", "package_placed", "supervisor_triage", "layer_built"}
    ]
    assert layer == GOLDEN_STORY_LAYER
    assert studio == GOLDEN_STUDIO
    found = [
        (r.trigger, r.subtask, r.role, r.kinds, r.category, r.owner, r.message, r.outcome, r.layer)
        for r in ledger
    ]
    assert found == GOLDEN_LEDGER
    assert golden_state(tmp_path) == GOLDEN_STATE  # built.01's keys, no open attempt, the runs named


async def test_a_layer_builds_continuation_nudges_are_the_recorded_texts(tmp_path: Path) -> None:
    await golden_layer_events(tmp_path)
    nudges = golden_prompts(tmp_path)
    # the brief, then the done-check, the fix round and the follow-up, each in its trace folder
    assert list(nudges) == ["build", "build/round-1", "build/round-2", "build/round-3"]
    assert nudges == GOLDEN_NUDGES


GOAL = "Add multiply: layer 1/1 Multiply"
BRIEF = "Add mul(a, b) to calc.py.\n\n## Acceptance criteria\n- mul(2, 3) is 6"
LAYER = {"test_cmd": PYTEST, "branch": "layer", "scope_tests": True}


def layer_studio(tmp_path: Path) -> tuple[StudioOrchestrator, Path]:
    """A studio on the calc repository with the golden script's coder and reviewer replies."""
    repo = calc_repo(tmp_path)
    engine = FakeEngine({role: list(SCRIPT_LAYER[role]) for role in ("architect", "reviewer")})
    return StudioOrchestrator(cfg_for(tmp_path, done_check=True), engine, run_id="layer"), repo


async def run_steps(orch: StudioOrchestrator, repo: Path, plan: Plan) -> RunReport:
    """A layer build as the step methods, called one at a time from outside with the state on the
    orchestrator, in the order the driver's `_build_once` and the layer bundle's nodes call them."""
    await orch.open_worktree(
        repo,
        base=None,
        branch=LAYER["branch"],
        detach=False,
        test_cmd=PYTEST,
        test_steps=(),
        scope_tests=True,
        checkpoints=None,
        max_steps=None,
        goal=GOAL,
        plan=plan,
        images=(),
    )
    await orch.place(None)
    chosen = await orch.plan_phase(GOAL, plan=plan, repo_map=False, map_focus=(), map_chars=None)
    assert chosen is not None
    run = await orch.coder_turn(chosen.subtasks[0])
    await orch.done_check(run)
    round_no = 0
    while True:
        review = await orch.review_round(run, round_no)
        if not orch.wants_fix(review, round_no):
            break
        assert review is not None
        await orch.fix_turn(run, review)
        round_no += 1
    orch.close_subtask(run)
    final = orch.results_text()  # before the gate's follow-up changes the coder's result, as today
    passed = await orch.gate_once(PYTEST)
    if orch.gate_follows_up(passed, "build"):
        text = orch.gate_tail("build")
        try:
            text = await orch.gate_advice("build", text)
        except WrongTest:
            passed = False
        else:
            await orch.follow_up("build", text)
            passed = await orch.gate_once(PYTEST)
            orch.settle_gate(passed, "build")
    return orch.finish(goal=GOAL, final=final, passed=passed, started=orch.started, branch=orch.branch)


def coder_prompts(orch: StudioOrchestrator) -> dict[str, str]:
    trace = orch.run_dir.path / "trace"
    return {
        str(f.parent.relative_to(trace)): same(json.loads(f.read_text())["messages"][-1]["content"])
        for f in sorted(trace.rglob("prompt.json"))
    }


async def test_the_steps_in_order_emit_what_execute_task_emits(tmp_path: Path) -> None:
    """Task 2: the studio's step methods, called one by one with the state on the orchestrator, build a layer
    as `execute_task` does with the driver's one-call arguments (the driver now calls the steps itself, so
    the comparison is made on the studio alone): the full neutralised events, the report and the coder's
    prompts."""
    plan = layer_plan(BRIEF, build_role="architect")
    one, repo_a = layer_studio(tmp_path / "a")
    a = await one.execute_task(
        GOAL, repo=repo_a, plan=plan, synthesise=False, gate_follow_up="build", repo_map=False, **LAYER
    )
    steps, repo_b = layer_studio(tmp_path / "b")
    b = await run_steps(steps, repo_b, plan)
    events_a = clean(kinds_all(a.run_dir.events_path), tmp_path / "a", drop=TIMING)
    events_b = clean(kinds_all(b.run_dir.events_path), tmp_path / "b", drop=TIMING)
    assert events_b == events_a
    seen = [e["kind"] for e in events_a]  # the script reaches every step, or the comparison proves little
    for kind in ("done_check_done", "review", "fix_round", "gate_failed", "follow_up", "gate_passed"):
        assert kind in seen, kind
    assert (b.final_answer, b.tests, b.winning_branch) == (a.final_answer, a.tests, a.winning_branch)
    assert a.tests == {"studio": True} and a.winning_branch == "layer"
    assert coder_prompts(steps) == coder_prompts(one)


async def opened(orch: StudioOrchestrator, repo: Path, plan: Plan) -> Plan:
    """`orch` started on `repo` with `plan` given, up to the plan: no map, no prepare."""
    await orch.open_worktree(
        repo,
        base=None,
        branch=None,
        detach=False,
        test_cmd="./check.sh",
        test_steps=(),
        scope_tests=False,
        checkpoints=None,
        max_steps=None,
        goal="goal",
        plan=plan,
        images=(),
    )
    chosen = await orch.plan_phase("goal", plan=plan, repo_map=False, map_focus=(), map_chars=None)
    assert chosen is not None
    return chosen


async def test_a_subtask_that_does_not_write_gets_no_done_check_or_review_from_the_steps(
    tmp_path: Path,
) -> None:
    """As `_run_subtask`: only a writer is done-checked and reviewed, though its role is in review.roles."""
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"docs": [DONE], "reviewer": [APPROVE]})
    cfg = cfg_for(tmp_path, done_check=True, review={"enabled": True, "roles": ["docs"], "rounds": 1})
    orch = StudioOrchestrator(cfg, engine, router=ChiefRouter())
    note = Subtask("note", "Describe it.\n\nAcceptance criteria: a README", "docs")
    chosen = await opened(orch, repo, Plan((), (note,)))
    run = await orch.coder_turn(chosen.subtasks[0])
    await orch.done_check(run)
    assert run.writes is False and await orch.review_round(run, 0) is None
    seen = [e["kind"] for e in kinds_all(orch.run_dir.events_path)]
    assert "done_check" not in seen and "review" not in seen
    assert [r.role for r in engine.requests] == ["docs"]


async def test_a_fix_round_that_raises_ends_its_subtask_as_an_engine_error_with_no_review_after(
    tmp_path: Path,
) -> None:
    """As in one call: a turn that raises is the subtask's engine_error result, and no review round follows
    it (each split step catches, and marks the run broken)."""
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {
            "chief": [PLAN, SYNTH],
            "architect": [WRITE_PASS, RUN, DONE],
            "docs": [DONE],
            "reviewer": [CHANGES, APPROVE],
        }
    )
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    real_run = orch.team.run

    async def run(ctx: Any, observer: Any) -> Any:
        if ctx.team_id == "fix" and ctx.attempt == 1:
            raise RuntimeError("the engine fell over")
        return await real_run(ctx, observer)

    orch.team.run = run  # type: ignore[method-assign]
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    ev = kinds_all(report.run_dir.events_path)
    assert [e["verdict"] for e in ev if e["kind"] == "review"] == ["request_changes"]
    [done] = [e for e in ev if e["kind"] == "subtask_done" and e["subtask"] == "fix"]
    assert (done["stop_reason"], done["fix_rounds"]) == ("engine_error", 1)


async def test_the_writing_steps_take_the_write_lock_themselves(tmp_path: Path) -> None:
    """The done-check, a review round and a fix round each commit or edit the worktree: called on their own,
    as the layer's nodes call them, each waits for the write lock as the coder turn does."""
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"architect": [WRITE_PASS, DONE, WRITE_PASS, DONE], "reviewer": [APPROVE]})
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    chosen = await opened(orch, repo, Plan((), (Subtask("fix", "make the check pass", "coder"),)))
    run = await orch.coder_turn(chosen.subtasks[0])

    async def waits(step: Awaitable[Any]) -> Any:
        async with orch.write_lock:
            task = asyncio.ensure_future(step)
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done()  # held off while another writer has the worktree
        return await task

    await waits(orch.done_check(run))
    review = await waits(orch.review_round(run, 0))
    assert review is not None and review.verdict == "approve"
    await waits(orch.fix_turn(run, review))
    assert run.fix_rounds == 1 and run.result is not None and run.result.stop_reason == "done"
