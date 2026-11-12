"""A layer's build as separate steps over one studio (generic nodes Part 2, "The layer bundle"): the glue that
sat around the one `execute_task` call in the driver's `_build_once`. `open_layer` starts the layer (its
studio run on record, the supervisor, the brief, the worktree with the package placed, the plan);
the studio's own steps run the coder, the done-check, the review and fix rounds and the gate
(`StudioOrchestrator.coder_turn` ... `settle_gate`); `layer_outcome` ends the run and says whether the layer
is built or goes back to the architect, and `squash_layer` records a built one. The driver's `_build_once`
calls them in order, and the layer bundle's check nodes call them one at a time (Task 5): the same events
either way.

`LayerStudio` is what they share: never saved (`state.json` holds only what `squash_layer` records), and
`close` ends it however the layer ended, so the supervisor's open ledger rows are closed `unknown` and the
worktree stays for the restart."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from airlie.story.asking import StoryBlocked
from airlie.story.board import Card, note
from airlie.story.brief_recipe import Brief, BriefInputs, assemble, load_recipe
from airlie.story.briefs import layer_goal, layer_plan
from airlie.story.context import CHARS_PER_TOKEN
from airlie.story.diagnosis import GATE_TAIL_LINES
from airlie.story.model import Layer, Story
from airlie.story.package import Package
from airlie.story.phase import decoding
from airlie.story.state import StoryState
from airlie.supervisor.layer import LayerSupervisor
from airlie.teams.worktree import (
    changed_files,
    commit_wip,
    delete_branch,
    remove_worktree,
    rev_parse,
    squash_since,
)
from airlie.topology.review import Review
from airlie.topology.studio import StudioOrchestrator, SubtaskRun
from airlie.topology.studio_plan import Plan

if TYPE_CHECKING:
    from airlie.story.pipeline import StoryPipeline


class LayerDied(RuntimeError):
    """A layer's outcome was asked for a build that never got that far (no coder turn, no final gate)."""


@dataclass
class LayerStudio:
    """One build of layer `nn` (index `i`, build `attempt`, the `last` of story.layer_attempts): its studio
    (`orch`), its supervisor (`sup`, None when off), `where` (its base ref and commit, its branch and its
    commit's author), its package, brief and plan, and what its steps have produced so far."""

    orch: StudioOrchestrator
    sup: LayerSupervisor | None
    nn: str
    i: int
    attempt: int
    last: bool
    where: tuple[str, str, str, Any]
    package: Package
    brief: Brief
    plan: Plan
    started: float
    run: SubtaskRun | None = None
    review: Review | None = None
    round_no: int = 0
    passed: bool | None = None
    follow_up_text: str | None = None
    final: str | None = None
    """The run's answer (`results_text`), taken after the coder's subtask is closed and before the gate: the
    gate's follow-up replaces the coder's result, and the report names the build's."""
    _decoding: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def worktree(self) -> Path:
        return self.orch.run_dir.path / "studio"

    async def leave_decoding(self) -> None:
        """The build lane's decode is over (`gap`): the planning lane may start a long turn. Idempotent."""
        await self._decoding.aclose()

    async def close(self) -> None:
        """The layer is over, however it ended: out of `decoding`, then the supervisor closed (its open rows
        `unknown`), after every subtask has finished, so an advice in flight is never cut off. Idempotent."""
        await self.leave_decoding()
        if not self._closed:
            self._closed = True
            if self.sup is not None:
                self.sup.close()


async def open_layer(
    pipe: StoryPipeline,
    st: StoryState,
    card: Card,
    story: Story,
    layers: tuple[Layer, ...],
    i: int,
    package: Package,
    where: tuple[str, str, str, Any],
    built: str,
    attempt: int,
) -> LayerStudio:
    """Start build `attempt` of layer `i` from `package` on a fresh studio run: the run on record before it
    starts (a run that dies is still named), `layer_start`, the supervisor, the coder's brief (`brief`
    event) and plan, then, decoding, the worktree on the layer's branch, the package placed and locked
    (`_place`) and the plan pinned. The brief shows the layer's files, so no repository map is built (brief
    recipes, 2026-11-01). Whatever raises on the way leaves the decode and closes the supervisor first."""
    base_ref, base_sha, branch, _ = where
    nn = f"{i + 1:02d}"
    layer = layers[i]
    orch = pipe.studio()
    pipe._layer_base = base_sha
    wt = orch.run_dir.path / "studio"
    st.runs.append(orch.run_dir.run_id)  # on record before it starts: a run that dies is still named
    if rev_parse(pipe.repo, f"refs/heads/{branch}") is None:
        # only a branch this attempt makes is recorded: a resume deletes what the attempt left, and a
        # branch that was already there (git then refuses to make it) is not airlie's to delete
        st.attempt = {"layer": nn, "branch": branch, "worktree": str(wt)}
    st.save()
    st.emit(
        "layer_start",
        layer=nn,
        branch=branch,
        base=base_ref,
        base_sha=base_sha,
        studio_run=orch.run_dir.run_id,
        attempt=attempt,
    )
    sup = pipe._supervisor(st, card, layer, nn, orch, wt, base_sha)
    orch.advisor = sup  # the gate retry and the repeat guard, in the studio and the team
    lane = AsyncExitStack()
    try:
        chars = pipe.cfg.roles[pipe.s.build_role].max_context * CHARS_PER_TOKEN
        inputs = BriefInputs(
            story, layers, i, package, pipe.repo, base_sha, chars, built=built, survey=st.survey or ""
        )
        brief = assemble("layer-build", load_recipe("layer-build"), inputs)
        st.emit("brief", brief="layer-build", layer=nn, chars=len(brief.text), sections=brief.sizes())
        given = layer_plan(brief.text, build_role=pipe.s.build_role)
        # the planning lane waits for the gap after (`leave_decoding`)
        await lane.enter_async_context(decoding(pipe.stories_dir, orch.run_dir.path))
        goal = layer_goal(story, layers, i)
        await orch.open_worktree(
            pipe.repo,
            base=base_sha,
            branch=branch,
            detach=False,
            test_cmd=pipe.test_cmd,
            test_steps=pipe.commands,
            scope_tests=True,
            checkpoints=None,
            max_steps=None,
            goal=goal,
            plan=given,
            images=(),
        )
        await orch.place(pipe._place(st, orch, package, nn))
        plan = await orch.plan_phase(goal, plan=given, repo_map=False, map_focus=(), map_chars=None)
        assert plan is not None  # a given plan is always the plan
    except BaseException:
        await lane.aclose()
        if sup is not None:
            sup.close()
        raise
    return LayerStudio(
        orch=orch,
        sup=sup,
        nn=nn,
        i=i,
        attempt=attempt,
        last=attempt >= pipe.s.layer_attempts,
        where=where,
        package=package,
        brief=brief,
        plan=plan,
        started=orch.started,
        _decoding=lane,
    )


async def layer_outcome(
    pipe: StoryPipeline,
    ls: LayerStudio,
    st: StoryState,
    card: Card,
    story: Story,
    layers: tuple[Layer, ...],
    *,
    squash: bool = True,
) -> str | None:
    """The end of the layer's studio run (its report and `run_done`, then out of the decode) and what came of
    it. A layer that changed nothing, or whose whole gate is still red, is a lesson and then, on the last
    build, StoryBlocked; else `layer_replanned` and what to tell the architect for the next package (the
    worktree and branch are removed so the layer's branch name is free again). A built layer is squashed
    and recorded (`squash_layer`, unless `squash` is False: the caller squashes it) and the answer is None.
    LayerDied when the build never reached its final gate."""
    from airlie.story.pipeline import (  # the pipeline imports this module
        _cut_off,
        _ending,
        _gate_detail,
        _kept,
        _redact_urls,
    )

    if ls.run is None or ls.final is None:
        missing = "coder turn" if ls.run is None else "final gate"
        raise LayerDied(f"layer {ls.nn}: its outcome was asked before its {missing} ran")
    orch, nn, layer = ls.orch, ls.nn, layers[ls.i]
    _, _, branch, _ = ls.where
    orch.finish(
        goal=layer_goal(story, layers, ls.i),
        final=ls.final,
        passed=ls.passed,
        started=orch.started,
        branch=orch.branch,
    )
    await ls.leave_decoding()
    wt = ls.worktree
    commit_wip(wt, f"studio: {branch} (final)")
    cut, kept = _cut_off(orch), _kept(wt, branch)
    ran = orch.runs.get("build")
    coder = ran.result if ran is not None else None
    attempt, last = ls.attempt, ls.last
    builds = f"{attempt} build{'' if attempt == 1 else 's'}"
    stopped = "" if attempt == 1 and last else f" after {builds}"
    failure: tuple[str, str, str, str] | None = None  # the ending, why, the reason, the evidence kind
    if not changed_files(wt, orch.lock_commit):
        reason = f"layer {nn} ({layer.title}) changed nothing{stopped}{cut}{kept}"
        failure = ("gate_red", "changed nothing", reason, "changed_nothing")
    elif ls.passed is not True:
        reason = f"layer {nn} ({layer.title}): `{pipe._shown_cmd}` fails on {branch}{stopped}{cut}{kept}"
        failure = (_ending(coder), _gate_detail(orch.gate_output), reason, "gate_fails")
    if failure is None:
        if squash:
            await squash_layer(pipe, ls, st, card, story, layers)
        return None
    ending, detail, reason, kind = failure
    if kind == "gate_fails":  # on record for the block's diagnosis, which is read from disk
        st.emit(
            "gate_tail",
            layer=nn,
            studio_run=orch.run_dir.run_id,
            output=_redact_urls("\n".join(orch.gate_output.strip().splitlines()[-GATE_TAIL_LINES:])),
        )
    pipe._lesson(st, nn, layer, ls.package, coder, ending, detail)
    if last:
        raise StoryBlocked(reason)
    st.emit("layer_replanned", layer=nn, attempt=attempt, why=ending, detail=detail)
    told = pipe._replan_feedback(st, orch, nn, coder)
    remove_worktree(pipe.repo, wt)
    delete_branch(pipe.repo, branch)
    st.attempt = None
    st.save()
    return told


async def squash_layer(
    pipe: StoryPipeline,
    ls: LayerStudio,
    st: StoryState,
    card: Card,
    story: Story,
    layers: tuple[Layer, ...],
) -> None:
    """A built layer squashed to one commit on its branch (with its provenance trailers), its worktree
    removed, and its record in `st.built`; `layer_built` and the note on the issue."""
    from airlie.story.pipeline import _rows  # the pipeline imports this module

    orch, nn, layer = ls.orch, ls.nn, layers[ls.i]
    total = f"{len(layers):02d}"
    _, base_sha, branch, author = ls.where
    wt = ls.worktree
    sha = squash_since(
        wt,
        base_sha,
        f"{story.title}: {layer.title} ({nn}/{total})",
        author=author,
        trailers=pipe._trailers(card, nn, total, orch.run_dir.run_id, ls.package),
    )
    remove_worktree(pipe.repo, wt)
    st.built[nn] = {
        "branch": branch,
        "sha": sha,
        "run_id": orch.run_dir.run_id,
        "subtasks": _rows(orch),
        "gate": pipe._shown_cmd,  # what this layer passed, for its PR: a resume can pass another
        "package": ls.package.to_dict(),
        "tests": list(ls.package.test_ids),
    }
    st.attempt = None
    st.save()
    st.emit("layer_built", layer=nn, branch=branch, sha=sha)
    note_text = f"layer {nn}/{total} built: {layer.title} (`{pipe._shown_cmd}` passes)"
    await pipe.board.comment(card, note(note_text), role=pipe.s.build_role)
