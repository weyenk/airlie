"""Studio topology (design 2026-10-13): the chief plans, the router assigns roles (and a skill), specialists
run as single agents on one worktree (writers serialised), a cross-family review gets one fix round, the
chief synthesises."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig, RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import ContentPart, EngineError, GenRequest, Message, image_part
from airlie.obs import EventSink, RunDir
from airlie.orchestrator import LoggingObserver
from airlie.repo_map import build_repo_map
from airlie.report import RunReport
from airlie.router import RouteDecision, Router, RoutingLog, build_router
from airlie.scheduler import SlotScheduler
from airlie.skills import KnowledgeIndex, load_skills, prompt_text, routable_roles, skill_criteria
from airlie.teams import NativeTeam, TeamContext, TeamOrchestrator, TeamResult
from airlie.teams.protocol import cut_off_text
from airlie.teams.worktree import add_worktree, changed_files, commit_wip, diff_between, head_sha
from airlie.tools import ToolContext, ToolRegistry, attach_decide
from airlie.tools.knowledge import SearchKnowledge
from airlie.tools.scope import GateMemo, autofix_steps, tree_fingerprint
from airlie.tools.shell import run_cmd, run_steps
from airlie.topology.review import Review, Reviewer, nudge_from_review
from airlie.topology.studio_plan import (
    Answer,
    Delegate,
    Plan,
    PlanError,
    Subtask,
    Synthesis,
    parse_chief,
    pin,
    schedule,
)

WRITE_TOOLS = {"write_file", "edit_file", "shell", "git"}
SKILL_QUESTION = "Which guideline should this specialist follow as the primary lens?"
FRESH_CONTINUATION = (
    "You have already worked on this subtask: your changes are in this worktree (see them with git diff, or "
    "read the files). Your earlier conversation is not repeated here. Pick up from the worktree as it is and "
    "deal with the following."
)
"""What a subtask sent back on a long conversation is told instead of seeing that conversation again."""
CHARS_PER_TOKEN = 4
"""A rough size of a stored conversation, without a tokenizer: about four characters to a token."""


def _estimated_tokens(state: dict[str, Any]) -> int:
    """About how many tokens the stored conversation `state` (NativeTeam's dumped messages) would resend."""
    chars = sum(
        len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or ""))
        for m in state.get("messages", [])
    )
    return chars // CHARS_PER_TOKEN


@dataclass
class SubtaskRun:
    subtask: Subtask
    role: str
    decision: RouteDecision
    skill: str | None = None
    result: TeamResult | None = None
    review: Review | None = None
    fix_rounds: int = 0
    preamble: str = ""  # what it ran with (the repo map, its skill's guideline): a follow-up runs with it too


@dataclass(frozen=True)
class PlanOutcome:
    """The chief's reply to a planning request: a plan to show a person, or a direct answer."""

    plan: Plan | None
    answer: str
    failed: bool


def specialists(cfg: AirlieConfig) -> dict[str, str]:
    """role -> summary for the roles the studio routes to, and a given plan may pin."""
    s = cfg.studio
    exclude = {
        s.chief_role,
        s.review.role,
        cfg.lts.summarizer.role,
        cfg.lts.controller.judge_role,
        cfg.aggregator.role,
        cfg.aggregator.judge_role,
        cfg.fork.hypothesis_role,
    }
    return routable_roles(cfg, exclude=exclude)


def check_plan(cfg: AirlieConfig, plan: Plan) -> None:
    """Raise PlanError when a plan from outside the chief (a person's edit, the pipeline) cannot run: no
    subtasks, more than studio.max_subtasks, a role that is not a specialist, a missing dependency, a
    duplicate id or a cycle. Every role is checked, pinned or not: a chief-made plan never comes here."""
    if not plan.subtasks:
        raise PlanError("the plan has no subtasks")
    limit = cfg.studio.max_subtasks
    if len(plan.subtasks) > limit:
        raise PlanError(f"the plan has {len(plan.subtasks)} subtasks; studio.max_subtasks is {limit}")
    roles = specialists(cfg)
    for s in plan.subtasks:
        if s.role_hint not in roles:
            use = ", ".join(sorted(roles))
            raise PlanError(f"subtask '{s.id}' names role {s.role_hint!r}; use one of: {use}")
    schedule(plan)


class StudioOrchestrator:
    def __init__(
        self,
        cfg: AirlieConfig,
        engine: Engine,
        *,
        run_id: str | None = None,
        team_factory: TeamOrchestrator | None = None,
        router: Router | None = None,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.run_dir = RunDir.create(cfg.observability.state_dir, run_id)
        self.sink = EventSink(self.run_dir.events_path, self.run_dir.run_id)
        self.team = team_factory or NativeTeam(engine)
        self.router = router or build_router(cfg)
        self.routing_log = RoutingLog(self.run_dir.path / "routing.jsonl")
        self.observer = LoggingObserver(self.sink, self.run_dir)
        self.scheduler = SlotScheduler(cfg.scheduler.max_active)
        self.write_lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(cfg.studio.parallel)
        self.preamble: str | None = None
        self.runs: dict[str, SubtaskRun] = {}
        self.images: list[Path] = []
        self.base_sha = ""
        self.worktree: Path | None = None
        self.test_cmd: str | None = None
        self.test_steps: tuple[str, ...] = ()  # the gate's commands, for run_tests to report one by one
        self.scope_base = ""  # a commit: run_tests then runs the gate's pytest on the tests changed since it
        self.gate_memo = (
            GateMemo()
        )  # the tree that last passed the whole gate, for run_tests and the final gate
        self.failed = False  # the chief never produced a runnable plan, or the engine failed on it
        self.criteria = specialists(cfg)

    # ---- chief ----
    async def _chief(self, user: str, *, with_images: bool) -> str:
        role_name = self.cfg.studio.chief_role
        role = self.cfg.roles[role_name]
        parts = None
        if with_images and self.images:
            parts = (ContentPart("text", text=user), *[image_part(p) for p in self.images])
        req = GenRequest(
            role=role_name,
            messages=[
                Message(role="system", content=prompt_text(role.prompt)),
                Message(role="user", content=user, parts=parts),
            ],
            sampling=role.sampling,
            max_tokens=role.max_tokens,
            thinking=role.thinking,
        )
        res = await self.engine.generate(req)
        if res.finish_reason == "length":
            # max_tokens ended it. With nothing to read (all thinking), or a plan, answer or synthesis cut off
            # inside its json (which parses to nothing, and would be taken for a prose answer), it is no reply
            empty = not res.text.strip()
            if empty or parse_chief(res.text) is None:
                raise EngineError(
                    cut_off_text(role_name, role, thinking=empty and bool(res.reasoning.strip()))
                )
        return res.text

    def _roles_table(self) -> str:
        return "\n".join(f"- {name}: {summary}" for name, summary in self.criteria.items())

    def _plan_message(self, goal: str) -> str:
        images = f"\nAttached images: {len(self.images)} (refer to them as img:1 …)." if self.images else ""
        head = f"{self.preamble}\n\n" if self.preamble else ""
        return (
            f"{head}## Request\n{goal}{images}\n\n## Specialists\n{self._roles_table()}\n\n"
            f"Reply with one JSON object: a plan of at most {self.cfg.studio.max_subtasks} subtasks "
            '({"type": "plan", "assumptions": [...], "subtasks": [{"id", "role", "brief", "depends_on", '
            '"inputs"}]}) or, if no specialist is needed, {"type": "answer", "answer": "..."}.'
        )

    def _synthesis_message(self, goal: str, *, rejected: str | None = None, final_round: bool = False) -> str:
        lines = [f"## Request\n{goal}", "## Subtask results"]
        for r in self.runs.values():
            res = r.result
            outcome = f"{res.stop_reason}, {res.steps} steps" if res else "not run"
            review = f"; review: {r.review.verdict} ({len(r.review.blocking)} blocking)" if r.review else ""
            text = (res.final_text if res else "")[: self.cfg.studio.result_chars]
            lines.append(f"### {r.subtask.id} ({r.role}; {outcome}{review})\n{text}")
        if rejected:
            lines.append(f"Your previous delegate was rejected: {rejected}")
        synth = 'Reply with one JSON object: {"type": "synthesis", "answer": "...", "open_questions": [...]}'
        if final_round:
            lines.append(f"{synth}. No more subtasks can run; synthesise what you have.")
        else:
            lines.append(
                f"{synth} or, for one more subtask, "
                '{"type": "delegate", "subtask": {"id", "role", "brief", "depends_on"}}.'
            )
        return "\n\n".join(lines)

    # ---- specialists ----
    def _tool_names(self, role: RoleSpec) -> list[str]:
        return list(self.cfg.tools.builtin) if role.tools is None else list(role.tools)

    def _tools(self, role: RoleSpec, test_cmd: str | None) -> ToolRegistry:
        assert self.worktree is not None
        names = [
            t for t in self._tool_names(role) if t not in ("decide", "search_knowledge", "retrieve_value")
        ]
        ctx = ToolContext(
            self.worktree,
            self.cfg.tools.shell_timeout_s,
            self.cfg.tools.output_cap_chars,
            test_cmd,
            self.test_steps,
            self.scope_base,
            self.gate_memo,
            self.cfg.tools.write_file_max_overwrite_lines,
        )
        reg = ToolRegistry.builtin(names, ctx)
        if role.tools and "decide" in role.tools:
            attach_decide(reg, self.cfg)
        if role.knowledge is not None and (role.tools is None or "search_knowledge" in role.tools):
            reg.add(SearchKnowledge(KnowledgeIndex.build(role.knowledge)))
        return reg

    def _writes(self, role: RoleSpec) -> bool:
        return bool(WRITE_TOOLS & set(self._tool_names(role)))

    def _brief_with_deps(self, s: Subtask) -> str:
        parts = [s.brief]
        for d in s.depends_on:
            r = self.runs.get(d)
            if r and r.result:
                text = r.result.final_text[: self.cfg.studio.result_chars]
                if r.result.ok:
                    parts.append(f"## Result of '{d}' ({r.role})\n{text}")
                else:
                    head = (
                        f"## Result of '{d}' ({r.role}; FAILED: {r.result.stop_reason}, treat as unreliable)"
                    )
                    parts.append(f"{head}\n{text}")
        return "\n\n".join(parts)

    def _fallback_role(self) -> str:
        team = self.cfg.teams.role
        return team if team in self.criteria else next(iter(self.criteria))

    async def _route(self, s: Subtask) -> tuple[str, RouteDecision]:
        if s.pinned and s.role_hint is not None and s.role_hint in self.criteria:
            # a person or a pipeline chose this role: the router gets no second opinion
            role = s.role_hint
            d = RouteDecision("role", role, role, "pinned", {role: 1.0}, 1.0, role)
        else:
            hint = s.role_hint if s.role_hint in self.criteria else None
            try:
                d = await self.router.route(s.brief, self.criteria, hint)
            except ValueError as e:  # chief router without a usable hint, or no fallback off the set
                role = self._fallback_role()
                d = RouteDecision("role", None, role, "fallback", {}, 0.0, s.role_hint, detail=str(e))
        self.routing_log.record(s.id, d)
        self.sink.emit(
            "route",
            subtask=s.id,
            chosen=d.chosen,
            decision=d.decision,
            confidence=d.confidence,
            probabilities=d.probabilities,
            hint=s.role_hint,
            criteria=sorted(self.criteria),
        )
        return d.chosen, d

    async def _skill(self, s: Subtask, role: RoleSpec) -> tuple[str | None, str]:
        if role.skills is None:
            return None, ""
        skills = load_skills(role.skills)
        if not skills:
            return None, ""
        try:
            d = await self.router.route(
                s.brief, skill_criteria(skills), None, question="skill", instructions=SKILL_QUESTION
            )
        except ValueError:  # a router that needs a hint (chief): run without a guideline
            self.sink.emit("skill", subtask=s.id, chosen=None, decision="none", confidence=0.0)
            return None, ""
        self.routing_log.record(f"{s.id}:skill", d)
        self.sink.emit("skill", subtask=s.id, chosen=d.chosen, decision=d.decision, confidence=d.confidence)
        chosen = [k for k in skills if k.name == d.chosen]
        if d.decision == "ambiguous":  # ai-mode: load the top two when unclear
            second = sorted(d.probabilities.items(), key=lambda kv: -kv[1])[1:2]
            chosen += [k for k in skills if second and k.name == second[0][0] and k not in chosen]
        return d.chosen, "\n\n".join(f"## Guideline: {k.name}\n{k.text}" for k in chosen)

    def _continuation(
        self, run: SubtaskRun, parent: TeamResult | None, nudge: str | None
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        """The conversation a subtask goes on in, its goal, and its nudge. A short one goes on as it is. One
        over teams.fresh_continuation_tokens starts again, so a fix round does not resend it (HDS-5: 58-72k
        tokens, and the coder's reply was cut off before it acted): the brief, where its work is, a nudge."""
        goal = self._brief_with_deps(run.subtask)
        state = parent.state if parent else None
        if state is None or nudge is None:
            return state, goal, nudge
        size = _estimated_tokens(state)
        if size <= self.cfg.teams.fresh_continuation_tokens:
            return state, goal, nudge
        self.sink.emit("fresh_continuation", subtask=run.subtask.id, role=run.role, estimated_tokens=size)
        return None, f"{goal}\n\n{FRESH_CONTINUATION}\n\n{nudge}", None

    def _context(
        self,
        run: SubtaskRun,
        role: RoleSpec,
        test_cmd: str | None,
        *,
        preamble: str,
        parent: TeamResult | None,
        nudge: str | None,
    ) -> TeamContext:
        assert self.worktree is not None
        state, goal, nudge = self._continuation(run, parent, nudge)
        return TeamContext(
            team_id=run.subtask.id,
            goal=goal,
            role=role,
            tools=self._tools(role, test_cmd),
            worktree=self.worktree,
            role_name=run.role,
            max_steps=role.max_steps or self.cfg.teams.max_steps,
            token_budget=self.cfg.teams.token_budget,
            scheduler=self.scheduler,
            preamble=preamble or None,
            initial_state=state,
            nudge=nudge,
            stop_when=lambda _s: False,  # keeps the conversation state for a fix round
            images=list(self.images) if role.vision else [],
            emit=self.sink.emit,
        )

    async def _run_subtask(self, run: SubtaskRun, test_cmd: str | None) -> None:
        role = self.cfg.roles[run.role]
        run.skill, skill_text = await self._skill(run.subtask, role)
        preamble = run.preamble = "\n\n".join(x for x in (self.preamble or "", skill_text) if x)
        writes = self._writes(role)
        self.sink.emit("subtask_start", subtask=run.subtask.id, role=run.role, skill=run.skill, writes=writes)
        ctx = self._context(run, role, test_cmd, preamble=preamble, parent=None, nudge=None)
        try:
            if writes:  # the lock first: a writer waiting its turn must not hold a parallel slot
                async with self.write_lock, self.sem:
                    assert self.worktree is not None
                    before = head_sha(self.worktree)  # this subtask's own base for the review diff
                    run.result = await self.team.run(ctx, self.observer)
                    await self._review_and_fix(run, role, test_cmd, preamble, before)
            else:
                async with self.sem:
                    run.result = await self.team.run(ctx, self.observer)
        except Exception as e:  # noqa: BLE001 - a broken subtask is reported, never a run-killer
            run.result = TeamResult(
                run.subtask.id, False, "engine_error", f"engine error: {type(e).__name__}: {e}", 0, 0, 0, 0.0
            )
        res = run.result
        assert res is not None
        self.sink.emit(
            "subtask_done",
            subtask=run.subtask.id,
            role=run.role,
            ok=res.ok,
            stop_reason=res.stop_reason,
            steps=res.steps,
            fix_rounds=run.fix_rounds,
        )

    async def _review_and_fix(
        self, run: SubtaskRun, role: RoleSpec, test_cmd: str | None, preamble: str, before: str
    ) -> None:
        rv = self.cfg.studio.review
        assert self.worktree is not None and run.result is not None
        if not rv.enabled or run.role not in rv.roles:
            return
        reviewer = Reviewer(self.engine, rv.role, self.cfg.roles[rv.role])
        for round_no in range(rv.rounds + 1):
            commit_wip(self.worktree, f"studio: {run.subtask.id} (round {round_no})")
            files = {
                p: (self.worktree / p).read_text(errors="replace")
                for p in changed_files(self.worktree, before)
                if (self.worktree / p).is_file()
            }
            try:
                review = await reviewer.review(
                    run.subtask.brief,
                    diff_between(self.worktree, before, rv.diff_chars),
                    run.result.last_test_output[-3000:],
                    files,
                    diff_chars=rv.diff_chars,
                    files_chars=rv.files_chars,
                )
            except EngineError as e:
                review = Review("error", (), f"engine error: {e}")
            run.review = review
            self.run_dir.write_trace(
                f"{run.subtask.id}/review-{round_no}.json",
                {"verdict": review.verdict, "findings": len(review.findings), "raw": review.raw},
            )
            self.sink.emit(
                "review",
                subtask=run.subtask.id,
                verdict=review.verdict,
                findings=len(review.findings),
                blocking=len(review.blocking),
                round=round_no,
            )
            if review.verdict != "request_changes" or not review.blocking or round_no >= rv.rounds:
                return
            run.fix_rounds += 1
            self.sink.emit(
                "fix_round", subtask=run.subtask.id, round=run.fix_rounds, blocking=len(review.blocking)
            )
            ctx = self._context(
                run, role, test_cmd, preamble=preamble, parent=run.result, nudge=nudge_from_review(review)
            )
            run.result = await self.team.run(ctx, self.observer)

    async def follow_up(self, subtask_id: str, nudge: str) -> None:
        """Send subtask `subtask_id`, which has run, back once more in its own conversation with `nudge` (as a
        fix round is), on the run's worktree, with no review: for a check that runs after the plan and before
        the gate (`execute_task`'s `before_gate`). An engine failure is its result, never raised."""
        run = self.runs[subtask_id]
        role = self.cfg.roles[run.role]
        self.sink.emit("follow_up", subtask=subtask_id, role=run.role)
        ctx = self._context(run, role, self.test_cmd, preamble=run.preamble, parent=run.result, nudge=nudge)
        try:
            async with self.write_lock, self.sem:
                run.result = await self.team.run(ctx, self.observer)
        except Exception as e:  # noqa: BLE001 - as in _run_subtask
            run.result = TeamResult(
                subtask_id, False, "engine_error", f"engine error: {type(e).__name__}: {e}", 0, 0, 0, 0.0
            )
        res = run.result
        self.sink.emit(
            "subtask_done",
            subtask=subtask_id,
            role=run.role,
            ok=res.ok,
            stop_reason=res.stop_reason,
            steps=res.steps,
            fix_rounds=run.fix_rounds,
        )

    async def _run_plan(self, plan: Plan, test_cmd: str | None) -> None:
        for wave in schedule(plan, satisfied=frozenset(self.runs)):
            runs: list[SubtaskRun] = []
            for s in wave:
                role, d = await self._route(s)
                runs.append(SubtaskRun(s, role, d))
                self.runs[s.id] = runs[-1]
            await asyncio.gather(*(self._run_subtask(r, test_cmd) for r in runs))

    # ---- the run ----
    def check_plan(self, plan: Plan) -> None:
        """Raise PlanError when a plan from outside the chief cannot run (see the module's `check_plan`)."""
        check_plan(self.cfg, plan)

    def _prepare(self, repo: Path, test_cmd: str | None) -> None:
        if not self.cfg.repo_map.enabled:
            return
        text, cached = build_repo_map(
            repo,
            test_cmd=test_cmd,
            max_chars=self.cfg.repo_map.max_chars,
            depth=self.cfg.repo_map.depth,
            cache_dir=self.cfg.observability.state_dir.expanduser() / "repo_maps",
        )
        self.preamble = text
        self.sink.emit("repo_map", chars=len(text), cached=cached)

    def _emit_plan(self, plan: Plan) -> None:
        self.sink.emit(
            "plan",
            assumptions=list(plan.assumptions),
            subtasks=[s.id for s in plan.subtasks],
            roles=[s.role_hint for s in plan.subtasks],
            pinned=any(s.pinned for s in plan.subtasks),
        )

    async def _make_plan(self, goal: str) -> tuple[Plan | None, str]:
        """The chief's plan, asked twice at most: (plan, "") or (None, the answer or why there is none)."""
        final = ""
        feedback: str | None = None
        for attempt in range(2):
            message = self._plan_message(goal)
            if feedback:
                message += f"\n\nYour previous plan was rejected: {feedback}"
            try:
                text = await self._chief(message, with_images=True)
            except EngineError as e:
                self.sink.emit("plan_error", attempt=attempt, error=f"engine error: {e}")
                final = f"engine error: {e}"
                self.failed = True
                continue
            parsed = parse_chief(text)
            if isinstance(parsed, Answer):
                self.failed = False
                return None, parsed.text
            if isinstance(parsed, Plan):
                try:
                    schedule(parsed)
                except PlanError as e:
                    self.sink.emit("plan_error", attempt=attempt, error=str(e))
                    feedback = str(e)
                    final = f"the chief did not produce a runnable plan: {e}"
                    self.failed = True
                    continue
                self.failed = False
                self._emit_plan(parsed)
                return parsed, ""
            self.sink.emit("plan_error", attempt=attempt, error="no plan or answer in the reply")
            final = text  # prose is the answer (design §4); not a failure
            self.failed = False
        return None, final

    async def plan(
        self, goal: str, *, repo: Path, test_cmd: str | None = None, images: Sequence[Path] = ()
    ) -> PlanOutcome:
        """The chief's plan for `goal`, not run. The interactive door shows it, a person edits it, and a later
        run takes the edited plan (`execute_task(plan=...)`)."""
        t0 = time.monotonic()
        self.images = list(images)
        self.sink.emit("run_start", goal=goal, k=1, repo=str(repo), orchestrator="studio", mode="plan")
        self._prepare(repo, test_cmd)
        plan, answer = await self._make_plan(goal)
        wall = round((time.monotonic() - t0) * 1000.0, 1)
        self.sink.emit("run_done", ok=not self.failed, wall_ms=wall, subtasks=0, tests=None)
        return PlanOutcome(plan, answer, self.failed)

    async def execute_task(
        self,
        query_text: str,
        *,
        repo: Path,
        test_cmd: str | None = None,
        images: Sequence[Path] = (),
        plan: Plan | None = None,
        base: str | None = None,
        branch: str | None = None,
        detach: bool = False,
        synthesise: bool = True,
        test_steps: Sequence[str] = (),
        before_gate: Callable[[], Awaitable[bool]] | None = None,
        scope_tests: bool = False,
    ) -> RunReport:
        """Plan, run, review, synthesise.

        `plan` replaces the chief's plan and pins its roles (PlanError before any work when it cannot run).
        `base` is the worktree's start point (default HEAD), `branch` its new branch (default
        airlie/<run>/studio), `detach` gives it none (read-only work). The repo map is built from that
        worktree, so every role sees the code it builds on, not the checkout it was started from.
        `synthesise=False` skips the chief's closing call: the answer is then each subtask's own result.
        `test_steps` are the gate's commands (`test_cmd` joined): run_tests reports each. `before_gate` runs
        once the plan has (it may `follow_up` a subtask); when it returns False the gate does not run and the
        tests count as failed. `scope_tests` makes run_tests run the gate's pytest on the test files the run
        changed first and the whole gate only once those pass; the final gate is skipped for a tree that
        passed it already."""
        given = pin(plan) if plan is not None else None
        if given is not None:
            self.check_plan(given)
        t0 = time.monotonic()
        self.images = list(images)
        self.sink.emit(
            "run_start",
            goal=query_text,
            k=1,
            repo=str(repo),
            orchestrator="studio",
            given_plan=given is not None,
        )
        name = None if detach else (branch or f"airlie/{self.run_dir.run_id}/studio")
        self.worktree = add_worktree(repo, self.run_dir.path / "studio", branch=name, start_point=base)
        self.base_sha = commit_wip(self.worktree, "studio: base")
        self.test_cmd, self.test_steps = test_cmd, tuple(test_steps)
        self.scope_base = self.base_sha if scope_tests else ""
        self.gate_memo = GateMemo()
        self._prepare(self.worktree, test_cmd)
        chosen: Plan | None = given
        final = ""
        if given is not None:
            self._emit_plan(given)
        else:
            chosen, final = await self._make_plan(query_text)
        if chosen is not None:
            await self._run_plan(chosen, test_cmd)
            final = await self._synthesise(query_text, test_cmd) if synthesise else self._results_text()
        checked = chosen is None or before_gate is None or await before_gate()
        passed = await self._tests_pass(test_cmd) if checked else False
        report = self._report(query_text, final, t0, name or "(detached)", passed)
        self.sink.emit(
            "run_done",
            ok=report.any_ok,
            wall_ms=round(report.wall_ms, 1),
            subtasks=len(self.runs),
            tests=passed,
        )
        return report

    def _results_text(self) -> str:
        """The answer without a synthesis: every subtask's own closing text, in run order."""
        return "\n\n".join(
            f"### {r.subtask.id} ({r.role})\n{r.result.final_text if r.result else '(not run)'}"
            for r in self.runs.values()
        )

    async def _synthesise(self, goal: str, test_cmd: str | None) -> str:
        """Synthesis rounds: a delegate runs one more subtask (its rejection is fed back); the last round
        forbids delegation so the run always ends with an answer."""
        rejected: str | None = None
        rounds = self.cfg.studio.max_rounds
        for round_no in range(rounds + 1):
            last = round_no == rounds
            message = self._synthesis_message(goal, rejected=rejected, final_round=last)
            try:
                text = await self._chief(message, with_images=False)
            except EngineError as e:
                self.sink.emit("plan_error", attempt=round_no, error=f"engine error: {e}")
                return f"engine error during synthesis: {e}"
            parsed = parse_chief(text)
            if isinstance(parsed, Delegate) and not last:
                sub = parsed.subtask
                unknown = [d for d in sub.depends_on if d not in self.runs]
                if unknown or sub.id in self.runs:
                    why = f"depends on {unknown}" if unknown else f"id '{sub.id}' already used"
                    rejected = f"delegate '{sub.id}' {why}"
                    self.sink.emit("plan_error", attempt=round_no, error=rejected)
                    continue
                rejected = None
                await self._run_plan(Plan((), (sub,)), test_cmd)
                continue
            final = parsed.answer if isinstance(parsed, Synthesis) else text
            open_q = list(parsed.open_questions) if isinstance(parsed, Synthesis) else []
            self.sink.emit("synthesis", chars=len(final), open_questions=open_q)
            return final
        return ""  # unreachable: the last round always returns

    async def _tests_pass(self, test_cmd: str | None) -> bool | None:
        """The test gate. The test command loads the code the roles wrote, so it runs as run_tests runs it
        (tools.shell.run_cmd): with the agent environment, and killed with all it started on a timeout or a
        cancel."""
        if not test_cmd or self.worktree is None or not self.runs:
            return None
        if (
            self.scope_base
        ):  # the coder's last edit may have come after its last run_tests: mend its style too
            fix = ToolContext(self.worktree, self.cfg.tools.shell_timeout_s, self.cfg.tools.output_cap_chars)
            await run_steps(autofix_steps(self.test_steps), fix)
        if (
            self.scope_base
            and self.gate_memo.passed is not None
            and tree_fingerprint(self.worktree) == self.gate_memo.passed
        ):
            return True  # run_tests already ran the whole gate on exactly this tree
        tools = self.cfg.tools
        ctx = ToolContext(self.worktree, tools.shell_timeout_s, tools.output_cap_chars, test_cmd)
        return (await run_cmd(test_cmd, ctx, shell=True)).ok

    def _report(self, goal: str, final: str, t0: float, branch: str, passed: bool | None) -> RunReport:
        teams = [r.result for r in self.runs.values() if r.result is not None]
        report = RunReport(self.run_dir.run_id, self.run_dir, goal, teams, (time.monotonic() - t0) * 1000.0)
        report.winner = "studio"
        report.final_answer = final
        report.winning_branch = branch
        report.tests = {"studio": passed}
        report.scores = {"studio": 1.0 if passed else 0.0}
        if self.failed:
            report.ok_override = False
        elif not teams:
            report.ok_override = bool(final)
        lines = [
            f"# airlie studio run {self.run_dir.run_id}",
            "",
            f"**Goal:** {goal}",
            "",
            "## Final answer",
            "",
            final,
            "",
            "## Subtasks",
            "",
            "| subtask | role | decision | skill | stop | steps | review | fix rounds |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in self.runs.values():
            res = r.result
            lines.append(
                f"| {r.subtask.id} | {r.role} | {r.decision.decision} | {r.skill or '-'} | "
                f"{res.stop_reason if res else '-'} | {res.steps if res else 0} | "
                f"{r.review.verdict if r.review else '-'} | {r.fix_rounds} |"
            )
        state = "pass" if passed else ("fail" if passed is False else "-")
        lines += ["", f"Tests on the worktree: {state}; branch `{branch}`."]
        (self.run_dir.path / "report.md").write_text("\n".join(lines) + "\n")
        return report
