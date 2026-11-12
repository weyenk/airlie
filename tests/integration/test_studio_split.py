"""The golden layer build (generic nodes Part 2, Task 1): a characterization of the studio's layer build as
the base commit's driver runs it, recorded before the studio is split into steps. It is green on the base
commit by design and every later task keeps it green: the same events, state, prompts and ledger rows, so
a refactor cannot change what a layer build does without this test saying so. The script (SCRIPT_LAYER)
drives the layer through every phase, and the first test pins that it still does: a refactor that skipped
a phase would otherwise pass by recording less."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from airlie.engine import FakeEngine
from airlie.report import RunReport
from airlie.router import ChiefRouter
from airlie.story.board import text_card
from airlie.supervisor.ledger import Row
from airlie.topology.studio import StudioOrchestrator, WrongTest
from tests.support.git import repo_with_marker_tests
from tests.support.layer_golden import (
    GOLDEN_LEDGER,
    GOLDEN_NUDGES,
    GOLDEN_STATE,
    GOLDEN_STORY_KINDS,
    GOLDEN_STORY_LAYER,
    GOLDEN_STUDIO,
    GOLDEN_STUDIO_KINDS,
    TIMING,
    build_run,
    clean,
    golden_layer_events,
    golden_prompts,
    golden_state,
    layer_engine,
    story_dir,
)
from tests.support.story import kinds_all, pipeline
from tests.support.story_layer_checks_flow import PYTEST, calc_repo
from tests.support.studio import APPROVE, CHANGES, DONE, PLAN, RUN, SYNTH, WRITE_PASS, cfg_for
from tests.support.supervisor_flow import SUPERVISOR_ROLES, rows


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
    assert one("steer", id="gate-failed")["role"] == "coder"
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


class Steps(StudioOrchestrator):
    """A studio whose layer build calls the step methods one at a time, from outside, as the layer's nodes
    will (Tasks 3 and 5); every other task it runs is `execute_task`'s."""

    async def execute_task(self, query_text: str, **kw: Any) -> RunReport:
        if kw.get("gate_follow_up") != "build":
            return await super().execute_task(query_text, **kw)
        started = time.monotonic()
        await self.open_worktree(
            kw["repo"],
            base=kw["base"],
            branch=kw["branch"],
            detach=False,
            test_cmd=kw["test_cmd"],
            test_steps=kw["test_steps"],
            scope_tests=kw["scope_tests"],
            checkpoints=None,
            max_steps=None,
            goal=query_text,
            plan=kw["plan"],
            images=(),
        )
        await self.place(kw["prepare"])
        plan = await self.plan_phase(
            query_text, plan=kw["plan"], repo_map=False, map_focus=(), map_chars=None
        )
        assert plan is not None
        run = await self.coder_turn(plan.subtasks[0])
        await self.done_check(run)
        round_no = 0
        while True:
            review = await self.review_round(run, round_no)
            if not self.wants_fix(review, round_no):
                break
            assert review is not None
            await self.fix_turn(run, review)
            round_no += 1
        self.close_subtask(run)
        final = self.results_text()  # before the gate's follow-up changes the coder's result, as today
        passed = await self.gate_once(kw["test_cmd"])
        if self.gate_follows_up(passed, "build"):
            text = self.gate_tail("build")
            try:
                text = await self.gate_advice("build", text)
            except WrongTest:
                passed = False
            else:
                await self.follow_up("build", text)
                passed = await self.gate_once(kw["test_cmd"])
                self.settle_gate(passed, "build")
        return self.finish(goal=query_text, final=final, passed=passed, started=started, branch=kw["branch"])


async def run_steps(tmp_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Row]]:
    """`golden_layer_events`, with the layer built by `Steps`."""
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
        studio=Steps,
    )
    st = await pipe.run(text_card("multiply"))
    assert st.blocked is None and list(st.built) == ["01"], st.blocked
    story = clean(kinds_all(story_dir(tmp_path) / "events.jsonl"), tmp_path)
    studio = clean(kinds_all(build_run(tmp_path) / "events.jsonl"), tmp_path, drop=TIMING)
    ledger = [
        replace(r, id=0, at=0.0, seconds=0.0, story="<story>", studio_run="<run>") for r in rows(tmp_path)
    ]
    return story, studio, ledger


async def test_the_steps_in_order_emit_what_execute_task_emits(tmp_path: Path) -> None:
    """Task 2: the studio's step methods, called one by one with the state on the orchestrator, build the
    golden layer as `execute_task` does: the full neutralised events of the build run and of the story, the
    ledger's rows and the coder's prompts."""
    a = await golden_layer_events(tmp_path / "a")
    b = await run_steps(tmp_path / "b")
    assert b[1] == a[1] == GOLDEN_STUDIO  # the build run's events, every field
    assert b[0] == a[0]  # the story's
    assert b[2] == a[2]  # the ledger's rows
    assert golden_prompts(tmp_path / "b") == golden_prompts(tmp_path / "a")


async def test_a_fix_round_that_raises_ends_its_subtask_as_an_engine_error_with_no_review_after(
    tmp_path: Path,
) -> None:
    """As in one call: a turn that raises is the subtask's engine_error result, and no review round follows
    it (each split step catches, and marks the run broken)."""
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {
            "chief": [PLAN, SYNTH],
            "coder": [WRITE_PASS, RUN, DONE],
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
