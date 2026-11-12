"""The layer build's glue (generic nodes Part 2, Task 3): `airlie.story.layer_build` opens a layer's studio
(`open_layer`), and the driver's `_build_once` is a wrapper that calls the studio's steps one at a time and
then `layer_outcome`, as the layer bundle's nodes will (Task 5). The golden layer (Task 1's script and
constants, recorded from the one-call build) is what the wrapper must still produce."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from airlie.report import RunReport
from airlie.story.board import Card, text_card
from airlie.story.layer_build import layer_outcome, open_layer
from airlie.story.model import Layer, Story
from airlie.story.package import Package
from airlie.story.phase import read_phase
from airlie.story.state import StoryState
from airlie.topology.studio import StudioOrchestrator, SubtaskRun
from airlie.topology.studio_plan import Subtask
from tests.support.layer_golden import (
    GOLDEN_LEDGER,
    GOLDEN_NUDGES,
    GOLDEN_STATE,
    GOLDEN_STORY_KINDS,
    GOLDEN_STORY_LAYER,
    GOLDEN_STUDIO,
    TIMING,
    build_run,
    clean,
    golden_prompts,
    golden_state,
    layer_engine,
    story_dir,
)
from tests.support.story import kinds, kinds_all, pipeline, worktrees
from tests.support.story_layer_checks_flow import PYTEST, calc_repo
from tests.support.supervisor_flow import SUPERVISOR_ROLES, answer, rows, scripted


class NoOneCall(StudioOrchestrator):
    """A studio whose `execute_task` refuses a layer build: the driver must build through the steps."""

    async def execute_task(self, query_text: str, **kw: Any) -> RunReport:
        plan = kw.get("plan")
        assert plan is None or [s.id for s in plan.subtasks] != ["build"], "a layer built in one call"
        return await super().execute_task(query_text, **kw)


async def test_build_once_through_the_wrapper_matches_the_golden_run(tmp_path: Path) -> None:
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
        studio=NoOneCall,
    )
    st = await pipe.run(text_card("multiply"))
    assert st.blocked is None and list(st.built) == ["01"], st.blocked
    story = clean(kinds_all(story_dir(tmp_path) / "events.jsonl"), tmp_path)
    studio = clean(kinds_all(build_run(tmp_path) / "events.jsonl"), tmp_path, drop=TIMING)
    ledger = [
        replace(r, id=0, at=0.0, seconds=0.0, story="<story>", studio_run="<run>") for r in rows(tmp_path)
    ]
    assert [e["kind"] for e in story] == GOLDEN_STORY_KINDS
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
    assert golden_state(tmp_path) == GOLDEN_STATE
    assert golden_prompts(tmp_path) == GOLDEN_NUDGES


class AdvisedThenDies(StudioOrchestrator):
    """A studio whose coder turn asks the supervisor (a ledger row opens, waiting for its outcome) and then
    raises past the turn's own catch, as a cancel or a bug outside the team does."""

    async def coder_turn(self, subtask: Subtask) -> SubtaskRun:
        assert self.advisor is not None
        await self.advisor.advise("gate_retry", subtask.id, "coder", "the gate is red", kinds=("gate_fails",))
        raise RuntimeError("the engine fell over")


async def test_a_raise_inside_the_coder_turn_closes_the_supervisors_rows_unknown_and_keeps_the_worktree(
    tmp_path: Path,
) -> None:
    repo = calc_repo(tmp_path)
    engine = scripted(coder=[], supervisor=[answer("Put add back as it was.")])
    pipe = pipeline(
        tmp_path,
        repo,
        engine,
        test_cmd=PYTEST,
        story={"supervisor": {"enabled": True}},
        roles=SUPERVISOR_ROLES,
        studio=AdvisedThenDies,
    )
    st = await pipe.run(text_card("multiply"))
    assert st.blocked == "RuntimeError: the engine fell over"
    [row] = [r for r in rows(tmp_path) if r.trigger == "gate_retry"]
    assert row.outcome == "unknown"  # closed by the layer's close, never left open
    assert st.attempt is not None and Path(st.attempt["worktree"]).is_dir()  # kept for the restart
    assert worktrees(repo) == 2 and st.built == {}
    assert read_phase(pipe.stories_dir)[0] != "decoding"  # the build lane left its decode
    assert "run_done" not in [e["kind"] for e in kinds_all(build_run(tmp_path) / "events.jsonl")]


async def test_layer_outcome_without_a_coder_turn_is_a_layer_died(tmp_path: Path) -> None:
    repo = calc_repo(tmp_path)
    pipe = pipeline(tmp_path, repo, scripted(coder=[]), test_cmd=PYTEST)

    async def outcome_first(
        st: StoryState,
        card: Card,
        story: Story,
        layers: tuple[Layer, ...],
        i: int,
        package: Package,
        where: tuple[str, str, str, Any],
        built: str,
        attempt: int,
    ) -> str | None:
        ls = await open_layer(pipe, st, card, story, layers, i, package, where, built, attempt)
        try:
            return await layer_outcome(pipe, ls, st, card, story, layers, squash=True)
        finally:
            await ls.close()

    pipe._build_once = outcome_first  # type: ignore[method-assign]
    st = await pipe.run(text_card("multiply"))
    assert st.blocked is not None and st.blocked.startswith("LayerDied: layer 01"), st.blocked
    assert st.built == {} and st.attempt is not None and worktrees(repo) == 2  # nothing squashed, kept
    studio = [e["kind"] for e in kinds_all(build_run(tmp_path) / "events.jsonl")]
    assert "package_placed" in [e["kind"] for e in kinds_all(st.path / "events.jsonl")]
    assert "subtask_start" not in studio and "run_done" not in studio  # no report for a layer that died
    assert kinds(st.path / "events.jsonl", "layer_built") == []
