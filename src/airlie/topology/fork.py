"""Fork-when-stuck (docs/superpowers/specs/2026-10-13-fork-when-stuck-design.md §3).

One trunk branch runs until it is stuck (after_failures consecutive failing run_tests, or a repeated action);
its working tree is committed, `width` children get worktrees at that commit and the trunk's conversation
verbatim plus one nudge each; the first child whose run_tests passes cancels its siblings and becomes the
trunk."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.obs import EventSink, RunDir
from airlie.orchestrator import LoggingObserver
from airlie.repo_map import build_repo_map
from airlie.report import RunReport, write_report
from airlie.scheduler import SlotScheduler
from airlie.teams import NativeTeam, StepObserver, StepRecord, TeamContext, TeamOrchestrator, TeamResult
from airlie.teams.worktree import add_worktree, commit_wip, diff_last_commit
from airlie.tools import ToolContext, ToolRegistry
from airlie.topology.diversify import (
    hypotheses,
    nudge_text,
    parse_failing,
    role_at_temperature,
    trim_repeated_tail,
)

FORK_REASONS: frozenset[str] = frozenset({"fork_point", "repeated_action"})
REPEAT_LIMIT = 3  # TeamContext.repeat_limit default: identical steps that make a repeated_action stop
ResolveHow = Literal["visible_pass", "best_effort", "none"]


class ForkObserver:
    """Tracks every branch's run_tests outcomes; fires on_pass when a branch's latest run passes."""

    def __init__(self, inner: StepObserver, sink: EventSink, *, after_failures: int) -> None:
        self.inner = inner
        self.sink = sink
        self.after_failures = after_failures
        self.failing: dict[str, int] = {}
        self.passed: dict[str, bool] = {}
        self.last_test: dict[str, str] = {}
        self.last_action: dict[str, tuple[str, str, str]] = {}
        self.on_pass: Callable[[str], None] | None = None

    async def before_step(self, team_id: str, step_num: int) -> str | None:
        return await self.inner.before_step(team_id, step_num)

    async def after_step(self, step: StepRecord) -> None:
        await self.inner.after_step(step)
        self.last_action[step.team_id] = (step.u_t.name, step.u_t.arguments, step.o_t)
        if step.u_t.name != "run_tests":
            return
        self.last_test[step.team_id] = step.o_t
        if step.ok:
            self.failing[step.team_id] = 0
            self.passed[step.team_id] = True
            if self.on_pass is not None:
                self.on_pass(step.team_id)
        else:
            self.failing[step.team_id] = self.failing.get(step.team_id, 0) + 1
            self.passed[step.team_id] = False

    def stuck(self, step: StepRecord) -> bool:
        return (
            step.u_t.name == "run_tests"
            and not step.ok
            and self.failing.get(step.team_id, 0) >= self.after_failures
        )


@dataclass
class Branch:
    id: str
    worktree: Path
    git_branch: str
    result: TeamResult | None = None
    hypothesis: str | None = None
    temperature: float | None = None
    nudge: str | None = None


def _cancelled(bid: str) -> TeamResult:
    return TeamResult(bid, False, "cancelled", "cancelled: a sibling passed first", 0, 0, 0, 0.0)


class ForkOrchestrator:
    def __init__(
        self,
        cfg: AirlieConfig,
        engine: Engine,
        *,
        run_id: str | None = None,
        team_factory: TeamOrchestrator | None = None,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.run_dir = RunDir.create(cfg.observability.state_dir, run_id)
        self.sink = EventSink(self.run_dir.events_path, self.run_dir.run_id)
        self.scheduler = SlotScheduler(cfg.scheduler.max_active, cfg.scheduler.starvation_ms)
        self.team: TeamOrchestrator = team_factory or self._default_team()
        self.observer = ForkObserver(
            LoggingObserver(self.sink, self.run_dir), self.sink, after_failures=cfg.fork.after_failures
        )
        self.preamble: str | None = None
        self.total_steps = 0
        self.branches: list[Branch] = []

    def _default_team(self) -> TeamOrchestrator:
        if self.cfg.teams.orchestrator == "autogen":
            from airlie.teams.autogen_team import AutoGenTeam

            role = self.cfg.roles[self.cfg.teams.role]
            e = self.cfg.engines[self.cfg.engine_for_model(role.model)]
            return AutoGenTeam(e.base_url or "", e.served_name(role.model), e.api_key)
        return NativeTeam(self.engine)

    # ---- run ----
    async def execute_task(self, query_text: str, *, repo: Path, test_cmd: str | None = None) -> RunReport:
        t0 = time.monotonic()
        f = self.cfg.fork
        self.sink.emit("run_start", goal=query_text, topology="fork", repo=str(repo), width=f.width)
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
        trunk = self._branch("trunk", repo, start_point=None)
        res = await self._run_branch(
            trunk, query_text, test_cmd, parent=None, max_steps=self.cfg.teams.max_steps
        )
        rounds = forks = 0
        while (
            self._stuck(trunk, res)
            and res.state is not None
            and rounds < f.max_rounds
            and self.total_steps < f.max_total_steps
        ):
            rounds += 1
            forks += 1
            base = commit_wip(trunk.worktree, f"airlie fork point {rounds}")
            kids = [self._branch(f"r{rounds}-{i + 1}", repo, start_point=base) for i in range(f.width)]
            await self._diversify(kids, query_text, trunk, res.last_test_output)
            repeated = (
                self.observer.last_action.get(trunk.id) if res.stop_reason == "repeated_action" else None
            )
            parent = res
            if repeated is not None:  # children must not inherit the stuck loop itself
                parent = replace(res, state=trim_repeated_tail(res.state, REPEAT_LIMIT))
            for k in kids:
                k.nudge = nudge_text(k.id, f.width, k.hypothesis, res.last_test_output, repeated)
            self.sink.emit(
                "fork_point",
                round=rounds,
                parent=trunk.id,
                reason=res.stop_reason,
                base=base,
                children=[k.id for k in kids],
                test_tail=res.last_test_output[-1500:],
                hypotheses=[k.hypothesis for k in kids],
                temperatures=[k.temperature for k in kids],
                repeated=(
                    {"name": repeated[0], "arguments": repeated[1], "output": repeated[2][-600:]}
                    if repeated
                    else None
                ),
            )
            await self._run_children(kids, query_text, test_cmd, parent=parent)
            winner, how = self._resolve(kids)
            self.sink.emit(
                "fork_resolved",
                round=rounds,
                winner=winner.id if winner else None,
                how=how,
                outcomes={k.id: (k.result.stop_reason if k.result else "none") for k in kids},
            )
            if winner is None or winner.result is None:
                break
            trunk, res = winner, winner.result
        report = self._report(query_text, trunk, res, t0, forks=forks, rounds=rounds)
        self.sink.emit(
            "run_done",
            ok=report.any_ok,
            wall_ms=round(report.wall_ms, 1),
            forks=forks,
            rounds=rounds,
            winner=report.winner,
            total_steps=self.total_steps,
        )
        return report

    def _stuck(self, b: Branch, res: TeamResult) -> bool:
        """Fork from a branch that hit a fork reason, or quit ("done") without a passing test run; never from
        a branch whose last test run passed."""
        if self.observer.passed.get(b.id, False):
            return False
        return res.stop_reason in FORK_REASONS or res.stop_reason == "done"

    # ---- branches ----
    def _branch(self, bid: str, repo: Path, *, start_point: str | None) -> Branch:
        git_branch = f"airlie/{self.run_dir.run_id}/{bid}"
        wt = add_worktree(repo, self.run_dir.path / bid, branch=git_branch, start_point=start_point)
        b = Branch(bid, wt, git_branch)
        self.branches.append(b)
        return b

    def _context(
        self, b: Branch, goal: str, test_cmd: str | None, *, max_steps: int, parent: TeamResult | None
    ) -> TeamContext:
        role_name = self.cfg.teams.role
        role = self.cfg.roles[role_name]
        if b.temperature is not None:
            role = role_at_temperature(role, b.temperature)
        tools = ToolRegistry.builtin(
            self.cfg.tools.builtin,
            ToolContext(
                b.worktree, self.cfg.tools.shell_timeout_s, self.cfg.tools.output_cap_chars, test_cmd
            ),
        )
        return TeamContext(
            team_id=b.id,
            goal=goal,
            role=role,
            tools=tools,
            worktree=b.worktree,
            role_name=role_name,
            max_steps=max_steps,
            token_budget=self.cfg.teams.token_budget,
            scheduler=self.scheduler,
            preamble=self.preamble,
            initial_state=parent.state if parent is not None else None,
            nudge=b.nudge,
            stop_when=self.observer.stuck,
        )

    async def _run_branch(
        self, b: Branch, goal: str, test_cmd: str | None, *, parent: TeamResult | None, max_steps: int
    ) -> TeamResult:
        self.sink.emit("branch_start", branch=b.id, worktree=str(b.worktree), child=parent is not None)
        ctx = self._context(b, goal, test_cmd, max_steps=max_steps, parent=parent)
        try:
            result = await self.team.run(ctx, self.observer)
        except asyncio.CancelledError:
            result = _cancelled(b.id)
        b.result = result
        self.total_steps += result.steps
        self.sink.emit(
            "branch_done",
            branch=b.id,
            ok=result.ok,
            stop_reason=result.stop_reason,
            steps=result.steps,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            wall_ms=round(result.wall_ms, 1),
            visible_pass=self.observer.passed.get(b.id, False),
        )
        return result

    async def _run_children(
        self, kids: list[Branch], goal: str, test_cmd: str | None, *, parent: TeamResult
    ) -> None:
        tasks = {
            k.id: asyncio.create_task(
                self._run_branch(k, goal, test_cmd, parent=parent, max_steps=self.cfg.fork.child_steps)
            )
            for k in kids
        }

        def on_pass(team_id: str) -> None:
            for kid, t in tasks.items():
                if kid != team_id and not t.done():
                    t.cancel()

        self.observer.on_pass = on_pass
        try:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        finally:
            self.observer.on_pass = None
        for k in kids:
            if k.result is None:  # cancelled before _run_branch could record it
                k.result = _cancelled(k.id)

    def _resolve(self, kids: list[Branch]) -> tuple[Branch | None, ResolveHow]:
        live = [
            k
            for k in kids
            if k.result is not None and k.result.stop_reason not in ("engine_error", "cancelled")
        ]
        passed = [k for k in live if self.observer.passed.get(k.id, False)]
        if passed:
            passed.sort(key=lambda k: (k.result.steps if k.result else 0, k.id))
            return passed[0], "visible_pass"
        if not live:
            return None, "none"

        def key(k: Branch) -> tuple[int, int, int, str]:
            """Fewest failing tests; then branches that ran tests at all; then the most work done."""
            failing = parse_failing(self.observer.last_test.get(k.id, ""))
            ran_tests = 0 if k.id in self.observer.last_test else 1
            steps = k.result.steps if k.result else 0
            return (failing if failing is not None else 10**6, ran_tests, -steps, k.id)

        live.sort(key=key)
        return live[0], "best_effort"

    async def _diversify(self, kids: list[Branch], goal: str, trunk: Branch, test_tail: str) -> None:
        f = self.cfg.fork
        hyps: list[str] | None = None
        if f.diversify == "hypotheses":
            role = self.cfg.roles[f.hypothesis_role]
            diff = diff_last_commit(trunk.worktree, max_chars=6000)
            hyps = await hypotheses(
                self.engine,
                f.hypothesis_role,
                role,
                goal=goal,
                diff=diff,
                test_tail=test_tail,
                width=len(kids),
            )
        for i, k in enumerate(kids):
            if hyps is not None:
                k.hypothesis = hyps[i]
            else:
                k.temperature = f.temperatures[i % len(f.temperatures)]

    # ---- report ----
    def _report(
        self, goal: str, trunk: Branch, res: TeamResult, t0: float, *, forks: int, rounds: int
    ) -> RunReport:
        if self.observer.passed.get(trunk.id) and not res.ok:  # tests pass: a result, not a failure
            res = replace(res, ok=True)
            trunk.result = res
        teams = [b.result for b in self.branches if b.result is not None]
        report = RunReport(self.run_dir.run_id, self.run_dir, goal, teams, (time.monotonic() - t0) * 1000.0)
        report.winner = trunk.id
        report.final_answer = res.final_text
        report.winning_branch = trunk.git_branch
        report.tests = {b.id: self.observer.passed.get(b.id) for b in self.branches}
        report.scores = {b.id: (1.0 if self.observer.passed.get(b.id) else 0.0) for b in self.branches}
        write_report(
            report, f"fork-when-stuck ({forks} forks, {rounds} rounds)", self.run_dir.path / "report.md"
        )
        return report
