"""Studio topology (design 2026-10-13): the chief plans, the router assigns roles (and a skill), specialists
run as single agents on one worktree (writers serialised), a cross-family review gets one fix round, the
chief synthesises."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
from airlie.supervisor.advice import with_advice
from airlie.teams import NativeTeam, TeamContext, TeamOrchestrator, TeamResult
from airlie.teams.protocol import StuckAdvisor, cut_off_text
from airlie.teams.worktree import (
    add_worktree,
    changed_files,
    changed_in_tree,
    commit_wip,
    diff_between,
    differs_from,
    dirty,
    find_file,
    head_sha,
    put_back,
    read_blob,
)
from airlie.tools import ToolContext, ToolRegistry, attach_decide
from airlie.tools.knowledge import SearchKnowledge
from airlie.tools.loop import DEFAULT_STEPS, generate_with_tools
from airlie.tools.scope import GateMemo, autofix_steps, tree_fingerprint
from airlie.tools.shell import run_cmd, run_steps
from airlie.tools.test_lock import touched, written_by_tester
from airlie.tools.web import SearXNGProvider, UrllibClient, WebFetch, WebSearch
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
DONE_CHECK = (
    "Before review: check your change against the brief's acceptance criteria and its exact wording. For "
    "each criterion, quote the lines of the diff below that make it true, or say 'not met'. Where the brief "
    "names "
    "a value, a message, a field, or which value to compare, check the diff uses exactly that. Anything not "
    "met or not exact: fix it now, run run_tests, then stop. If everything is met, reply with the checklist "
    "and stop."
)
"""The done-check: a coder that stopped 'done' reads its own diff against the criteria before the reviewer
does (AIR-31 layer 02, 2026-10-19: the reviewer's blocking finding was a value the brief had named)."""
GATE_FAILED = (
    "Your tests pass, but the whole suite fails with your change: the output is below. Fix the code so the "
    "whole suite passes; the acceptance tests stay as they are. Run run_tests, then stop."
)
"""What the subtask named by execute_task's `gate_follow_up` is told when the final gate fails."""
CHARS_PER_TOKEN = 4
"""A rough size of a stored conversation, without a tokenizer: about four characters to a token."""


def _estimated_tokens(state: dict[str, Any]) -> int:
    """About how many tokens the stored conversation `state` (NativeTeam's dumped messages) would resend."""
    chars = sum(
        len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or ""))
        for m in state.get("messages", [])
    )
    return chars // CHARS_PER_TOKEN


class Advisor(StuckAdvisor, Protocol):
    """A second reader for the studio's retries (the supervisor, in a story run): it may add a diagnosis to
    what a role is sent back with, and hears what came of it. As the team's repeat guard it is a StuckAdvisor
    too."""

    async def advise(
        self,
        trigger: str,
        subtask: str,
        role: str,
        told: str,
        *,
        kinds: Sequence[str] = (),
        evidence: str = "",
    ) -> str | None:
        """The advice to add to `told`; None for none."""
        ...

    def settle(self, trigger: str, outcome: str, detail: str = "", *, subtask: str | None = None) -> None:
        """What came of the latest advice for `trigger` (and `subtask`)."""
        ...


@dataclass
class SubtaskRun:
    subtask: Subtask
    role: str
    decision: RouteDecision
    skill: str | None = None
    result: TeamResult | None = None
    review: Review | None = None
    fix_rounds: int = 0
    rounds: int = 0
    """Continuations run so far (a done-check, fix rounds, a follow-up): the next one's attempt number, which
    names its trace folder, whether or not the conversation starts again."""
    preamble: str = ""  # what it ran with (the repo map, its skill's guideline): a follow-up runs with it too
    before: str = (
        ""  # the worktree's head when it started: its review diff and its later runs' already_written
    )


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
        self.observer = LoggingObserver(self.sink, self.run_dir, topology="studio")
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
        self.step_limit: int | None = (
            None  # steps per subtask for this run, over the role's (planning rounds)
        )
        self.scope_base = ""  # a commit: run_tests then runs the gate's pytest on the tests changed since it
        self.gate_memo = GateMemo()  # the tree that last passed the final gate: asked again, it does not run
        self.locked: frozenset[str] = frozenset()  # files only `lock_owner` may write (`lock`)
        self.locked_whole: frozenset[str] = frozenset()  # of those, the files locked whole
        self.locked_tests: dict[str, frozenset[str]] = {}  # the rest: the definitions the tester wrote
        self.lock_owner = ""
        self.lock_commit = ""
        self.checkpoints: Mapping[str, Callable[[], Awaitable[bool]]] = {}
        self.halted = False  # a checkpoint said no: the subtasks after it did not run
        self.amend_tests: Callable[[], Awaitable[bool]] | None = None  # execute_task's `amend_tests`
        self.failed = False  # the chief never produced a runnable plan, or the engine failed on it
        self.criteria = specialists(cfg)
        self.advisor: Advisor | None = None  # the supervisor, in a story run: the gate retry and repeat guard

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
        res = await generate_with_tools(
            self.engine, req, self._lookup_tools(role_name), role.max_steps or DEFAULT_STEPS
        )
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

    def _tools(self, role: RoleSpec, test_cmd: str | None, role_name: str | None = None) -> ToolRegistry:
        assert self.worktree is not None
        role_name = role_name or ""
        names = [
            t
            for t in self._tool_names(role)
            if t not in ("decide", "search_knowledge", "retrieve_value", "web_search", "web_fetch")
        ]
        ctx = ToolContext(
            self.worktree,
            self.cfg.tools.shell_timeout_s,
            self.cfg.tools.output_cap_chars,
            test_cmd,
            self.test_steps,
            self.scope_base,
            write_file_max_overwrite_lines=self.cfg.tools.write_file_max_overwrite_lines,
            run_dir=self.run_dir.path,
            role=role_name,
            read_only=self.locked_whole if role_name != self.lock_owner else frozenset(),
            locked_tests=self.locked_tests if role_name != self.lock_owner else {},
            **self.cfg.tools.confinement(),
        )
        reg = ToolRegistry.builtin(names, ctx)
        if role.tools and "decide" in role.tools:
            attach_decide(reg, self.cfg)
        if role.knowledge is not None and (role.tools is None or "search_knowledge" in role.tools):
            reg.add(SearchKnowledge(KnowledgeIndex.build(role.knowledge)))
        if "web_search" in self._tool_names(role):
            reg.add(WebSearch(SearXNGProvider(self.cfg.web.search.url, UrllibClient())))
        if "web_fetch" in self._tool_names(role):
            reg.add(WebFetch(timeout_s=self.cfg.web.fetch_timeout_s))
        return reg

    def _lookup_tools(self, role_name: str) -> ToolRegistry | None:
        """What a single-call role (the chief, the reviewer) may look up before it answers: its tools, when it
        has any (web_search and web_fetch: config validation refuses them beside a mutating tool), run in a
        bounded loop (tools.loop). None when it has none, and for a run with no worktree yet."""
        role = self.cfg.roles[role_name]
        if not role.tools or self.worktree is None:
            return None
        return self._tools(role, None, role_name)

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
        attempt: int = 0,
    ) -> TeamContext:
        assert self.worktree is not None
        state, goal, nudge = self._continuation(run, parent, nudge)
        # a fix round or follow-up continues work that is in the worktree (committed before the review), and
        # a first run may start on what the subtasks it depends on left there (a tester's tests): the write
        # nudges must count those files, not ask for them again (AIR-7, 2026-10-18; AIR-30, 2026-10-19)
        written = tuple(changed_in_tree(self.worktree, run.before)) if run.before else ()
        return TeamContext(
            team_id=run.subtask.id,
            goal=goal,
            role=role,
            tools=self._tools(role, test_cmd, run.role),
            worktree=self.worktree,
            role_name=run.role,
            max_steps=self.step_limit or role.max_steps or self.cfg.teams.max_steps,
            token_budget=self.cfg.teams.token_budget,
            scheduler=self.scheduler,
            preamble=preamble or None,
            initial_state=state,
            nudge=nudge,
            already_written=written,
            attempt=attempt,  # its steps' trace folder, whether or not the conversation starts again
            must_act=True,  # a build's deliverable is the change: a first reply of prose is sent back once
            stop_when=lambda _s: False,  # keeps the conversation state for a fix round
            images=list(self.images) if role.vision else [],
            emit=self.sink.emit,
            on_stuck=self.advisor,
        )

    async def _run_subtask(self, run: SubtaskRun, test_cmd: str | None) -> None:
        role = self.cfg.roles[run.role]
        run.skill, skill_text = await self._skill(run.subtask, role)
        preamble = run.preamble = "\n\n".join(x for x in (self.preamble or "", skill_text) if x)
        writes = self._writes(role)
        self.sink.emit("subtask_start", subtask=run.subtask.id, role=run.role, skill=run.skill, writes=writes)
        try:
            if writes:  # the lock first: a writer waiting its turn must not hold a parallel slot
                async with self.write_lock, self.sem:
                    assert self.worktree is not None
                    before = head_sha(self.worktree)  # this subtask's own base for the review diff
                    run.before = before
                    ctx = self._context(run, role, test_cmd, preamble=preamble, parent=None, nudge=None)
                    run.result = await self.team.run(ctx, self.observer)
                    await self._done_check(run, role, test_cmd, preamble, before)
                    await self._review_and_fix(run, role, test_cmd, preamble, before)
            else:
                async with self.sem:
                    ctx = self._context(run, role, test_cmd, preamble=preamble, parent=None, nudge=None)
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

    @staticmethod
    def _next_attempt(run: SubtaskRun) -> int:
        run.rounds += 1
        return run.rounds

    async def _done_check(
        self, run: SubtaskRun, role: RoleSpec, test_cmd: str | None, preamble: str, before: str
    ) -> None:
        """A coder that stopped 'done' on a brief with acceptance criteria is sent its diff with DONE_CHECK:
        a miss it finds is fixed in a short continuation before the reviewer charges a round for it."""
        res = run.result
        assert res is not None and self.worktree is not None
        s = self.cfg.studio
        if (
            not s.done_check
            or run.role not in s.review.roles  # it stands in for a review round: a role with none has none
            or res.stop_reason != "done"
            or "acceptance criteria" not in run.subtask.brief.lower()
        ):
            return
        wip = commit_wip(self.worktree, f"studio: {run.subtask.id} (done-check)")
        diff = diff_between(self.worktree, before, s.review.diff_chars)
        if not diff.strip():  # nothing changed: the review says so
            return
        nudge = f"{DONE_CHECK}\n\n## Your diff\n```diff\n{diff}\n```"
        ctx = self._context(
            run, role, test_cmd, preamble=preamble, parent=res, nudge=nudge, attempt=self._next_attempt(run)
        )
        ctx.max_steps = s.done_check_steps
        ctx.must_act = False  # a checklist that finds everything met is the answer
        self.sink.emit("done_check", subtask=run.subtask.id, role=run.role, fresh=ctx.initial_state is None)
        run.result = await self.team.run(ctx, self.observer)
        self.sink.emit(
            "done_check_done",
            subtask=run.subtask.id,
            stop_reason=run.result.stop_reason,
            steps=run.result.steps,
            changed=dirty(self.worktree) or head_sha(self.worktree) != wip,
        )

    async def _review_and_fix(
        self, run: SubtaskRun, role: RoleSpec, test_cmd: str | None, preamble: str, before: str
    ) -> None:
        rv = self.cfg.studio.review
        assert self.worktree is not None and run.result is not None
        if not rv.enabled or run.role not in rv.roles:
            return
        reviewer = Reviewer(self.engine, rv.role, self.cfg.roles[rv.role], self._lookup_tools(rv.role))
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
                run,
                role,
                test_cmd,
                preamble=preamble,
                parent=run.result,
                nudge=nudge_from_review(review),
                attempt=self._next_attempt(run),
            )
            run.result = await self.team.run(ctx, self.observer)

    async def follow_up(self, subtask_id: str, nudge: str) -> None:
        """Send subtask `subtask_id`, which has run, back once more in its own conversation with `nudge` (as a
        fix round is), on the run's worktree, with no review: for a check that runs after the plan and before
        the gate (`execute_task`'s `before_gate`). An engine failure is its result, never raised."""
        run = self.runs[subtask_id]
        role = self.cfg.roles[run.role]
        self.sink.emit("follow_up", subtask=subtask_id, role=run.role)
        ctx = self._context(
            run,
            role,
            self.test_cmd,
            preamble=run.preamble,
            parent=run.result,
            nudge=nudge,
            attempt=self._next_attempt(run),  # after the done-check and fix rounds, if any
        )
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

    def lock(self, paths: Sequence[str], owner: str) -> None:
        """From now on only role `owner` may write `paths` (from the worktree's root): the file tools refuse
        them to every other role, and the final gate puts them back as they are now, whatever a shell
        did to them. The worktree is committed first: that commit is what they are put back to.

        A Python file is locked by definition (AIR-9 layer 05, 2026-10-22: a whole-file lock kept the coder
        from fixing the fakes in a file the tester had only added tests to): only the top-level functions
        and classes the tester added or changed since the base are locked. Any other file, and a Python
        file that does not parse, is locked whole."""
        assert self.worktree is not None
        self.lock_commit = commit_wip(self.worktree, f"studio: {owner} (locked)")
        whole: set[str] = set()
        tests: dict[str, frozenset[str]] = {}
        for path in sorted(set(paths)):
            names = None
            if path.endswith(".py"):
                text = self._text_at(self.lock_commit, path)
                if text is not None:
                    names = written_by_tester(self._text_at(self.base_sha, path), text)
            if names is None:
                whole.add(path)
            else:
                tests[path] = names
        self.locked, self.lock_owner = frozenset(paths), owner
        self.locked_whole, self.locked_tests = frozenset(whole), tests
        self.sink.emit(
            "locked",
            owner=owner,
            paths=sorted(self.locked),
            tests={p: sorted(n) for p, n in sorted(tests.items())},
        )

    def _text_at(self, commit: str, path: str) -> str | None:
        """`path`'s text in `commit`; None when the commit has no such file."""
        assert self.worktree is not None
        if not commit:
            return None
        kind, sha = find_file(self.worktree, commit, path)
        return read_blob(self.worktree, sha) if kind == "blob" else None

    def _restore_locked(self) -> None:
        """The locked files as `lock` found them: a change to one (a shell's, since the file tools refuse) is
        undone before the gate, and an event names it. A file locked by definition is put back whole only
        when one of the tester's definitions changed or the file no longer parses; other edits stay."""
        if not self.locked or self.worktree is None:
            return
        changed = differs_from(self.worktree, self.lock_commit, sorted(self.locked))
        back = [p for p in changed if p not in self.locked_tests]
        for path in changed:
            names = self.locked_tests.get(path)
            if names is None:
                continue
            was = self._text_at(self.lock_commit, path) or ""
            file = self.worktree / path
            now = file.read_text(errors="replace") if file.is_file() else None
            if now is None or touched(names, was, now) != []:
                back.append(path)
        if back:
            put_back(self.worktree, self.lock_commit, back)
            self.sink.emit("locked_restored", paths=back)

    async def _run_plan(self, plan: Plan, test_cmd: str | None) -> None:
        for wave in schedule(plan, satisfied=frozenset(self.runs)):
            if self.halted:
                return
            runs: list[SubtaskRun] = []
            for s in wave:
                role, d = await self._route(s)
                runs.append(SubtaskRun(s, role, d))
                self.runs[s.id] = runs[-1]
            await asyncio.gather(*(self._run_subtask(r, test_cmd) for r in runs))
            for r in runs:
                check = self.checkpoints.get(r.subtask.id)
                if check is not None and not await check():
                    self.halted = True
                    self.sink.emit("halted", after=r.subtask.id)
                    return

    # ---- the run ----
    def check_plan(self, plan: Plan) -> None:
        """Raise PlanError when a plan from outside the chief cannot run (see the module's `check_plan`)."""
        check_plan(self.cfg, plan)

    def _prepare(self, repo: Path, test_cmd: str | None, focus: Sequence[str] = ()) -> None:
        if not self.cfg.repo_map.enabled:
            return
        text, cached = build_repo_map(
            repo,
            test_cmd=test_cmd,
            max_chars=self.cfg.repo_map.max_chars,
            depth=self.cfg.repo_map.depth,
            cache_dir=self.cfg.observability.state_dir.expanduser() / "repo_maps",
            focus=focus,
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
        checkpoints: Mapping[str, Callable[[], Awaitable[bool]]] | None = None,
        gate_follow_up: str | None = None,
        map_focus: Sequence[str] = (),
        amend_tests: Callable[[], Awaitable[bool]] | None = None,
        max_steps: int | None = None,
    ) -> RunReport:
        """Plan, run, review, synthesise.

        `plan` replaces the chief's plan and pins its roles (PlanError before any work when it cannot run).
        `base` is the worktree's start point (default HEAD), `branch` its new branch (default
        airlie/<run>/studio), `detach` gives it none (read-only work). The repo map is built from that
        worktree, so every role sees the code it builds on, not the checkout it was started from.
        `synthesise=False` skips the chief's closing call: the answer is then each subtask's own result.
        `test_steps` are the gate's commands (`test_cmd` joined): run_tests reports each. `before_gate` runs
        once the plan has (it may `follow_up` a subtask); when it returns False the gate does not run and the
        tests count as failed. `scope_tests` makes run_tests run the gate with pytest limited to the test
        files the run changed: the whole gate is the final one. `checkpoints` maps a subtask's id to a check
        that runs once it is done, before the subtasks after it (it may `follow_up` it, or `lock` its files);
        one that returns False stops the plan there, and the tests count as failed. `gate_follow_up` names the
        subtask sent the output of a failing final gate once (GATE_FAILED), before the gate runs again.
        `amend_tests` runs first when that gate fails and that subtask finished: it may send the locked tests'
        author back to mend its own tests (and `lock` them again), and returns whether it changed them; when
        it did, the gate runs again before the follow-up.
        `max_steps` limits every subtask's steps in this run, follow-ups included."""
        self.step_limit = max_steps
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
        self.checkpoints, self.halted = dict(checkpoints or {}), False
        self.amend_tests = amend_tests
        self._prepare(self.worktree, test_cmd, map_focus)
        chosen: Plan | None = given
        final = ""
        if given is not None:
            self._emit_plan(given)
        else:
            chosen, final = await self._make_plan(query_text)
        if chosen is not None:
            await self._run_plan(chosen, test_cmd)
            final = await self._synthesise(query_text, test_cmd) if synthesise else self._results_text()
        checked = not self.halted and (chosen is None or before_gate is None or await before_gate())
        passed = await self._gate(test_cmd, gate_follow_up) if checked else False
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

    async def _gate(self, test_cmd: str | None, follow_up: str | None) -> bool | None:
        """The final gate (`_tests_pass`); when it fails and `follow_up` names a subtask that finished, the
        locked tests may be mended first (`amend_tests`, then the gate again), and if it still fails that
        subtask is sent its output once (GATE_FAILED) and the gate runs again."""
        passed, output = await self._tests_pass(test_cmd)
        run = self.runs.get(follow_up) if follow_up is not None else None
        # only a subtask that finished is sent back: one cut off or broken has its own stop to report
        if passed is not False or run is None or run.result is None or not run.result.ok:
            return passed
        assert follow_up is not None
        # a coder cannot change a wrong acceptance test (AIR-9 layer 02, 2026-10-21): its author can
        if self.amend_tests is not None and await self.amend_tests():
            passed, output = await self._tests_pass(test_cmd)
            if passed is not False:
                return passed
        self.sink.emit("gate_failed", follow_up=follow_up)
        tail = output[-self.cfg.tools.output_cap_chars :]
        text = f"{GATE_FAILED}\n\n```\n{tail}\n```"
        if self.advisor is not None:
            try:
                # `text` carries the gate's tail already: it is not sent a second time as evidence
                advice = await self.advisor.advise(
                    "gate_retry", follow_up, run.role, text, kinds=("gate_fails",)
                )
            except Exception:  # noqa: BLE001 - an advisor that fails leaves GATE_FAILED as it is, never a stop
                advice = None
            text = with_advice(text, advice)
        await self.follow_up(follow_up, text)
        passed = (await self._tests_pass(test_cmd))[0]
        if self.advisor is not None:
            with contextlib.suppress(Exception):  # an outcome it cannot record does not touch the run
                self.advisor.settle("gate_retry", "helped" if passed else "not_helped", subtask=follow_up)
        return passed

    async def _tests_pass(self, test_cmd: str | None) -> tuple[bool | None, str]:
        """The test gate, and its output. The test command loads the code the roles wrote, so it runs as
        run_tests runs it (tools.shell.run_cmd): with the agent environment, and killed with all it started
        on a timeout or a cancel. Locked files are put back first (`lock`). A tree that passed it already
        (`gate_memo`) passes without running it again."""
        if not test_cmd or self.worktree is None or not self.runs:
            return None, ""
        self._restore_locked()
        if self.scope_base:  # the last edit may have come after the last run_tests: mend its style too
            fix = ToolContext(
                self.worktree,
                self.cfg.tools.shell_timeout_s,
                self.cfg.tools.output_cap_chars,
                **self.cfg.tools.confinement(),
            )
            await run_steps(autofix_steps(self.test_steps), fix)
        passed = self.gate_memo.passed  # fingerprinted only when there is a pass to compare with
        if passed is not None and tree_fingerprint(self.worktree) == passed:
            return True, "the whole gate already passed on exactly this tree"
        tools = self.cfg.tools
        ctx = ToolContext(
            self.worktree, tools.shell_timeout_s, tools.output_cap_chars, test_cmd, **tools.confinement()
        )
        result = await run_cmd(test_cmd, ctx, shell=True)
        if result.ok:
            self.gate_memo.passed = tree_fingerprint(self.worktree)
        return result.ok, result.output

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
