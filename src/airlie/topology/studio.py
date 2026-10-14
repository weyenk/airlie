"""Studio topology (design 2026-10-13): the chief plans, the router assigns roles (and a skill), specialists
run as single agents on one worktree (writers serialised), a cross-family review gets one fix round, the
chief synthesises."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from airlie.config import AirlieConfig, RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import ContentPart, GenRequest, Message, image_part
from airlie.obs import EventSink, RunDir
from airlie.orchestrator import LoggingObserver
from airlie.repo_map import build_repo_map
from airlie.report import RunReport
from airlie.router import RouteDecision, Router, RoutingLog, build_router
from airlie.scheduler import SlotScheduler
from airlie.skills import KnowledgeIndex, load_skills, prompt_text, routable_roles, skill_criteria
from airlie.teams import NativeTeam, TeamContext, TeamOrchestrator, TeamResult
from airlie.teams.worktree import add_worktree, changed_files, commit_wip, diff_between
from airlie.tools import ToolContext, ToolRegistry, attach_decide
from airlie.tools.knowledge import SearchKnowledge
from airlie.topology.review import Review, Reviewer, nudge_from_review
from airlie.topology.studio_plan import (
    Answer,
    Delegate,
    Plan,
    PlanError,
    Subtask,
    Synthesis,
    parse_chief,
    schedule,
)

WRITE_TOOLS = {"write_file", "edit_file", "shell", "git"}
SKILL_QUESTION = "Which guideline should this specialist follow as the primary lens?"


@dataclass
class SubtaskRun:
    subtask: Subtask
    role: str
    decision: RouteDecision
    skill: str | None = None
    result: TeamResult | None = None
    review: Review | None = None
    fix_rounds: int = 0


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
        self.criteria = routable_roles(cfg, exclude=exclude)

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
        return (await self.engine.generate(req)).text

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

    def _synthesis_message(self, goal: str) -> str:
        lines = [f"## Request\n{goal}", "## Subtask results"]
        for r in self.runs.values():
            res = r.result
            outcome = f"{res.stop_reason}, {res.steps} steps" if res else "not run"
            review = f"; review: {r.review.verdict} ({len(r.review.blocking)} blocking)" if r.review else ""
            text = (res.final_text if res else "")[: self.cfg.studio.result_chars]
            lines.append(f"### {r.subtask.id} ({r.role}; {outcome}{review})\n{text}")
        lines.append(
            'Reply with one JSON object: {"type": "synthesis", "answer": "...", "open_questions": [...]} or, '
            'for one more subtask, {"type": "delegate", "subtask": {"id", "role", "brief", "depends_on"}}.'
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
            self.worktree, self.cfg.tools.shell_timeout_s, self.cfg.tools.output_cap_chars, test_cmd
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
                parts.append(f"## Result of '{d}' ({r.role})\n{text}")
        return "\n\n".join(parts)

    async def _route(self, s: Subtask) -> tuple[str, RouteDecision]:
        hint = s.role_hint if s.role_hint in self.criteria else None
        d = await self.router.route(s.brief, self.criteria, hint)
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
        d = await self.router.route(
            s.brief, skill_criteria(skills), None, question="skill", instructions=SKILL_QUESTION
        )
        self.routing_log.record(f"{s.id}:skill", d)
        self.sink.emit("skill", subtask=s.id, chosen=d.chosen, decision=d.decision, confidence=d.confidence)
        chosen = [k for k in skills if k.name == d.chosen]
        if d.decision == "ambiguous":  # ai-mode: load the top two when unclear
            second = sorted(d.probabilities.items(), key=lambda kv: -kv[1])[1:2]
            chosen += [k for k in skills if second and k.name == second[0][0] and k not in chosen]
        return d.chosen, "\n\n".join(f"## Guideline: {k.name}\n{k.text}" for k in chosen)

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
        return TeamContext(
            team_id=run.subtask.id,
            goal=self._brief_with_deps(run.subtask),
            role=role,
            tools=self._tools(role, test_cmd),
            worktree=self.worktree,
            role_name=run.role,
            max_steps=role.max_steps or self.cfg.teams.max_steps,
            token_budget=self.cfg.teams.token_budget,
            scheduler=self.scheduler,
            preamble=preamble or None,
            initial_state=parent.state if parent else None,
            nudge=nudge,
            stop_when=lambda _s: False,  # keeps the conversation state for a fix round
            images=list(self.images) if role.vision else [],
        )

    async def _run_subtask(self, run: SubtaskRun, test_cmd: str | None) -> None:
        role = self.cfg.roles[run.role]
        run.skill, skill_text = await self._skill(run.subtask, role)
        preamble = "\n\n".join(x for x in (self.preamble or "", skill_text) if x)
        writes = self._writes(role)
        self.sink.emit("subtask_start", subtask=run.subtask.id, role=run.role, skill=run.skill, writes=writes)
        ctx = self._context(run, role, test_cmd, preamble=preamble, parent=None, nudge=None)
        if writes:  # the lock first: a writer waiting its turn must not hold a parallel slot
            async with self.write_lock, self.sem:
                run.result = await self.team.run(ctx, self.observer)
                await self._review_and_fix(run, role, test_cmd, preamble)
        else:
            async with self.sem:
                run.result = await self.team.run(ctx, self.observer)
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
        self, run: SubtaskRun, role: RoleSpec, test_cmd: str | None, preamble: str
    ) -> None:
        rv = self.cfg.studio.review
        assert self.worktree is not None and run.result is not None
        if not rv.enabled or run.role not in rv.roles:
            return
        reviewer = Reviewer(self.engine, rv.role, self.cfg.roles[rv.role])
        before = self.base_sha
        for round_no in range(rv.rounds + 1):
            commit_wip(self.worktree, f"studio: {run.subtask.id} (round {round_no})")
            files = {
                p: (self.worktree / p).read_text(errors="replace")
                for p in changed_files(self.worktree, before)
                if (self.worktree / p).is_file()
            }
            review = await reviewer.review(
                run.subtask.brief,
                diff_between(self.worktree, before, rv.diff_chars),
                run.result.last_test_output[-3000:],
                files,
                diff_chars=rv.diff_chars,
                files_chars=rv.files_chars,
            )
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

    async def _run_plan(self, plan: Plan, test_cmd: str | None) -> None:
        for wave in schedule(plan, satisfied=frozenset(self.runs)):
            runs: list[SubtaskRun] = []
            for s in wave:
                role, d = await self._route(s)
                runs.append(SubtaskRun(s, role, d))
                self.runs[s.id] = runs[-1]
            await asyncio.gather(*(self._run_subtask(r, test_cmd) for r in runs))

    # ---- the run ----
    async def execute_task(
        self,
        query_text: str,
        *,
        repo: Path,
        test_cmd: str | None = None,
        images: Sequence[Path] = (),
    ) -> RunReport:
        t0 = time.monotonic()
        self.images = list(images)
        self.sink.emit("run_start", goal=query_text, k=1, repo=str(repo), orchestrator="studio")
        if self.cfg.repo_map.enabled:
            text, cached = build_repo_map(
                repo,
                test_cmd=test_cmd,
                max_chars=self.cfg.repo_map.max_chars,
                depth=self.cfg.repo_map.depth,
                cache_dir=self.cfg.observability.state_dir.expanduser() / "repo_maps",
            )
            self.preamble = text
            self.sink.emit("repo_map", chars=len(text), cached=cached)
        branch = f"airlie/{self.run_dir.run_id}/studio"
        self.worktree = add_worktree(repo, self.run_dir.path / "studio", branch=branch)
        self.base_sha = commit_wip(self.worktree, "studio: base")
        final = ""
        plan: Plan | None = None
        for attempt in range(2):
            text = await self._chief(self._plan_message(query_text), with_images=True)
            parsed = parse_chief(text)
            if isinstance(parsed, Answer):
                final = parsed.text
                break
            if isinstance(parsed, Plan):
                try:
                    schedule(parsed)
                except PlanError as e:
                    self.sink.emit("plan_error", attempt=attempt, error=str(e))
                    final = text
                    continue
                plan = parsed
                self.sink.emit(
                    "plan",
                    assumptions=list(plan.assumptions),
                    subtasks=[s.id for s in plan.subtasks],
                    roles=[s.role_hint for s in plan.subtasks],
                )
                break
            self.sink.emit("plan_error", attempt=attempt, error="no plan or answer in the reply")
            final = text
        if plan is not None:
            await self._run_plan(plan, test_cmd)
            for round_no in range(self.cfg.studio.max_rounds):
                text = await self._chief(self._synthesis_message(query_text), with_images=False)
                parsed = parse_chief(text)
                if isinstance(parsed, Delegate):
                    sub = parsed.subtask
                    unknown = [d for d in sub.depends_on if d not in self.runs]
                    if unknown or sub.id in self.runs:
                        why = f"depends on {unknown}" if unknown else f"id '{sub.id}' already used"
                        self.sink.emit("plan_error", attempt=round_no, error=f"delegate '{sub.id}' {why}")
                        continue
                    await self._run_plan(Plan((), (sub,)), test_cmd)
                    continue
                final = parsed.answer if isinstance(parsed, Synthesis) else text
                open_q = list(parsed.open_questions) if isinstance(parsed, Synthesis) else []
                self.sink.emit("synthesis", chars=len(final), open_questions=open_q)
                break
        passed = await self._tests_pass(test_cmd)
        report = self._report(query_text, final, t0, branch, passed)
        self.sink.emit(
            "run_done",
            ok=report.any_ok,
            wall_ms=round(report.wall_ms, 1),
            subtasks=len(self.runs),
            tests=passed,
        )
        return report

    async def _tests_pass(self, test_cmd: str | None) -> bool | None:
        if not test_cmd or self.worktree is None or not self.runs:
            return None
        proc = await asyncio.create_subprocess_shell(
            test_cmd, cwd=self.worktree, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=self.cfg.tools.shell_timeout_s)
        except TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0

    def _report(self, goal: str, final: str, t0: float, branch: str, passed: bool | None) -> RunReport:
        teams = [r.result for r in self.runs.values() if r.result is not None]
        report = RunReport(self.run_dir.run_id, self.run_dir, goal, teams, (time.monotonic() - t0) * 1000.0)
        report.winner = "studio"
        report.final_answer = final
        report.winning_branch = branch
        report.tests = {"studio": passed}
        report.scores = {"studio": 1.0 if passed else 0.0}
        if not teams:
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
