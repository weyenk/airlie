"""ParallelAgentTeamOrchestrator (docs/wiki/build-spec/04 §3): K teams in parallel over one engine.

M2: teams run in isolated worktrees with sandboxed tools, every step is logged; the StepObserver is a no-op.
M3 replaces the observer with the LTS substrate; M4 adds the aggregator."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.obs import EventSink, RunDir
from airlie.scheduler import HALSpeculativeScheduler, SlotScheduler
from airlie.teams import NativeTeam, StepObserver, StepRecord, TeamContext, TeamOrchestrator, TeamResult
from airlie.teams.worktree import add_worktree
from airlie.tools import ToolContext, ToolRegistry


@dataclass
class RunReport:
    run_id: str
    run_dir: RunDir
    goal: str
    teams: list[TeamResult] = field(default_factory=list)
    wall_ms: float = 0.0

    @property
    def any_ok(self) -> bool:
        return any(t.ok for t in self.teams)


class LoggingObserver:
    """M2 observer: writes every step to events.jsonl and a trace file. M3 wraps this with LTS."""

    def __init__(self, sink: EventSink, run_dir: RunDir) -> None:
        self.sink = sink
        self.run_dir = run_dir

    async def before_step(self, team_id: str, step_num: int) -> str | None:
        return None

    async def after_step(self, step: StepRecord) -> None:
        self.sink.emit(
            "step",
            team_id=step.team_id,
            step=step.step_num,
            tool=step.u_t.name,
            ok=step.ok,
            prompt_tokens=step.prompt_tokens,
            completion_tokens=step.completion_tokens,
            gen_ms=round(step.gen_ms, 1),
            tool_ms=round(step.tool_ms, 1),
        )
        self.run_dir.write_trace(
            f"{step.team_id}/step-{step.step_num:03d}.json",
            {
                "u_t": {"name": step.u_t.name, "arguments": step.u_t.arguments},
                "o_t": step.o_t,
                "ok": step.ok,
                "assistant_text": step.assistant_text,
            },
        )


class ParallelAgentTeamOrchestrator:
    def __init__(
        self,
        cfg: AirlieConfig,
        engine: Engine,
        *,
        run_id: str | None = None,
        team_factory: TeamOrchestrator | None = None,
        observer: StepObserver | None = None,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.run_dir = RunDir.create(cfg.observability.state_dir, run_id)
        self.sink = EventSink(self.run_dir.events_path, self.run_dir.run_id)
        self.scheduler = SlotScheduler(cfg.scheduler.max_active, cfg.scheduler.starvation_ms)
        self.hal = HALSpeculativeScheduler()
        self.team: TeamOrchestrator = team_factory or self._default_team()
        self.observer: StepObserver = observer or LoggingObserver(self.sink, self.run_dir)

    def _default_team(self) -> TeamOrchestrator:
        if self.cfg.teams.orchestrator == "autogen":
            from airlie.teams.autogen_team import AutoGenTeam

            role = self.cfg.roles[self.cfg.teams.role]
            e = self.cfg.engines[self.cfg.engine_for_model(role.model)]
            return AutoGenTeam(e.base_url or "", e.served_name(role.model), e.api_key)
        return NativeTeam(self.engine)

    async def run_step(self, team_id: str, prompt: str, history: list[dict[str, str]]) -> str:
        """Spec 04 §3 blueprint entry point: one ReAct step. M2 teams run whole trajectories; kept for M3."""
        raise NotImplementedError("M3: run_step with LTS injection")

    async def execute_task(self, query_text: str, *, repo: Path, test_cmd: str | None = None) -> RunReport:
        t0 = time.monotonic()
        k = self.cfg.teams.k
        self.sink.emit(
            "run_start", goal=query_text, k=k, repo=str(repo), orchestrator=self.cfg.teams.orchestrator
        )
        teams = [f"team-{i + 1}" for i in range(k)]
        results = await asyncio.gather(*(self._run_team(tid, query_text, repo, test_cmd) for tid in teams))
        report = RunReport(
            self.run_dir.run_id, self.run_dir, query_text, list(results), (time.monotonic() - t0) * 1000.0
        )
        m = self.scheduler.metrics()
        self.sink.emit(
            "run_done",
            ok=report.any_ok,
            wall_ms=round(report.wall_ms, 1),
            yields=m.yield_count,
            max_concurrent=m.max_concurrent,
            slot_wait_ms=round(m.slot_wait_ms_total, 1),
            hal_budgets=self.hal.allocate_budgets(teams),
        )
        return report

    async def _run_team(self, team_id: str, goal: str, repo: Path, test_cmd: str | None) -> TeamResult:
        wt = add_worktree(repo, self.run_dir.path / team_id, branch=f"airlie/{self.run_dir.run_id}/{team_id}")
        role_name = self.cfg.teams.role
        role = self.cfg.roles[role_name]
        tools = ToolRegistry.builtin(
            self.cfg.tools.builtin,
            ToolContext(wt, self.cfg.tools.shell_timeout_s, self.cfg.tools.output_cap_chars, test_cmd),
        )
        ctx = TeamContext(
            team_id=team_id,
            goal=goal,
            role=role,
            tools=tools,
            worktree=wt,
            role_name=role_name,
            max_steps=self.cfg.teams.max_steps,
            token_budget=self.cfg.teams.token_budget,
            scheduler=self.scheduler,
        )
        self.sink.emit("team_start", team_id=team_id, worktree=str(wt))
        result = await self.team.run(ctx, self.observer)
        self.hal.update_hal(
            team_id, 0
        )  # accepted draft tokens are reported only by engines with per_request_draft
        self.sink.emit(
            "team_done",
            team_id=team_id,
            ok=result.ok,
            stop_reason=result.stop_reason,
            steps=result.steps,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            wall_ms=round(result.wall_ms, 1),
        )
        self.run_dir.write_trace(
            f"{team_id}/result.json", {"final_text": result.final_text, "stop_reason": result.stop_reason}
        )
        return result
