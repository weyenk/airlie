"""ParallelAgentTeamOrchestrator (docs/wiki/build-spec/04 §3): K teams in parallel over one engine.

M2: teams run in isolated worktrees with sandboxed tools, every step is logged; the StepObserver is a no-op.
M3 replaces the observer with the LTS substrate; M4 adds the aggregator."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.engine.types import GenRequest, Message, ToolCall
from airlie.lts import MemoryEntry, SharedMemoryBank
from airlie.lts.controller import AdmissionContext, build_controller
from airlie.lts.inject import render_keys
from airlie.lts.log import AdmissionLog
from airlie.lts.memory_tool import RetrieveValue
from airlie.lts.observer import LTSObserver
from airlie.lts.summarizer import build_summarizer
from airlie.obs import EventSink, RunDir
from airlie.repo_map import build_repo_map
from airlie.report import RunReport, write_report
from airlie.scheduler import HALSpeculativeScheduler, SlotScheduler
from airlie.skills import prompt_text
from airlie.subproc import agent_env, kill_group, started
from airlie.teams import NativeTeam, StepObserver, StepRecord, TeamContext, TeamOrchestrator, TeamResult
from airlie.teams.aggregate import Aggregator, LLMAggregator, TeamOutcome, TestScoreAggregator
from airlie.teams.worktree import add_worktree
from airlie.tools import ToolContext, ToolRegistry


class HALObserver:
    """Spec 04 §1 in the loop: update A_i from accepted draft tokens after each step, re-allocate budgets."""

    def __init__(
        self,
        hal: HALSpeculativeScheduler,
        scheduler: SlotScheduler,
        sink: EventSink,
        inner: StepObserver,
        teams: list[str],
    ) -> None:
        self.hal = hal
        self.scheduler = scheduler
        self.sink = sink
        self.inner = inner
        self.teams = teams
        self.budgets: dict[str, int] = hal.allocate_budgets(teams)

    async def before_step(self, team_id: str, step_num: int) -> str | None:
        return await self.inner.before_step(team_id, step_num)

    async def after_step(self, step: StepRecord) -> None:
        await self.inner.after_step(step)
        if step.accepted_draft_tokens is not None:
            self.hal.update_hal(step.team_id, step.accepted_draft_tokens)
            active = self.scheduler.active_ids() or self.teams
            self.budgets = self.hal.allocate_budgets(sorted(set(active) | {step.team_id}))
            self.sink.emit(
                "hal",
                team_id=step.team_id,
                step=step.step_num,
                accepted=step.accepted_draft_tokens,
                drafts=step.draft_tokens,
                hal=round(self.hal.hal_history[step.team_id], 3),
                budgets=self.budgets,
            )

    def budget_for(self, team_id: str) -> int | None:
        return self.budgets.get(team_id)


class LoggingObserver:
    """M2 observer: writes every step to events.jsonl and a trace file. M3 wraps this with LTS."""

    def __init__(
        self,
        sink: EventSink,
        run_dir: RunDir,
        *,
        model: str | None = None,
        role_name: str | None = None,
        topology: str | None = None,
    ) -> None:
        self.sink = sink
        self.run_dir = run_dir
        self.model = model
        self.role_name = role_name
        self.topology = topology
        self.run_span_id = uuid.uuid4().hex  # one run-level span: every step's parent

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
            prefill_ms=round(step.prefill_ms, 1),
            gen_ms=round(step.gen_ms, 1),
            tool_ms=round(step.tool_ms, 1),
            model=step.model or self.model,
            role=step.role or self.role_name,
            topology=self.topology,
            story=self.run_dir.story_key,
            span_id=uuid.uuid4().hex,
            parent_id=self.run_span_id,
        )
        # a later attempt (a fix round) counts its steps from 1 again: its own folder, named as its review
        folder = step.team_id if step.attempt == 0 else f"{step.team_id}/round-{step.attempt}"
        self.run_dir.write_trace(
            f"{folder}/step-{step.step_num:03d}.json",
            {
                "u_t": {"name": step.u_t.name, "arguments": step.u_t.arguments},
                "o_t": step.o_t,
                "ok": step.ok,
                "assistant_text": step.assistant_text,
                "reasoning": step.reasoning,
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
        # Spec 04 §3: memory_bank, controller (C_θ), scheduler
        self.memory_bank = SharedMemoryBank(persist_path=self.run_dir.path / "memory.jsonl")
        self.controller = build_controller(cfg.lts.controller, engine)
        self.summarizer = build_summarizer(cfg.lts.summarizer, engine)
        self.lts_log = AdmissionLog(self.run_dir.path / "lts")
        self.query = ""
        role = self.cfg.roles[self.cfg.teams.role]
        self._logging = LoggingObserver(
            self.sink,
            self.run_dir,
            model=role.model,
            role_name=self.cfg.teams.role,
            topology=self.cfg.topology.kind,
        )
        self.observer: StepObserver = observer or self._logging
        self.lts: LTSObserver | None = None
        self.hal_observer: HALObserver | None = None
        self.preamble: str | None = None
        self.aggregator: Aggregator = (
            LLMAggregator(engine, cfg.aggregator.role)
            if cfg.aggregator.kind == "llm"
            else TestScoreAggregator()
        )

    def _default_team(self) -> TeamOrchestrator:
        if self.cfg.teams.orchestrator == "autogen":
            from airlie.teams.autogen_team import AutoGenTeam

            role = self.cfg.roles[self.cfg.teams.role]
            e = self.cfg.engines[self.cfg.engine_for_model(role.model)]
            return AutoGenTeam(e.base_url or "", e.served_name(role.model), e.api_key)
        return NativeTeam(self.engine)

    def evaluate_lts_admission(self, query: str, summary: str, output: str) -> bool:
        """Spec 04 §3 signature; synchronous wrapper over the configured controller C_θ."""
        import asyncio

        ctx = AdmissionContext(
            query=query,
            memory_keys=[k for _, k in self.memory_bank.get_summaries()],
            u_t=ToolCall("", "generate", ""),
            s_t=summary,
            o_t=output,
            team_id="",
            step_num=0,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.controller.decide(ctx)).admit
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: asyncio.run(self.controller.decide(ctx))).result().admit

    async def run_step(self, team_id: str, prompt: str, history: list[dict[str, str]]) -> str:
        """Spec 04 §3 blueprint: inject selective LTS keys, generate one step, evaluate admission, store."""
        summaries = self.memory_bank.get_summaries()
        summary_prompt = render_keys(summaries, max_keys=self.cfg.lts.max_keys)
        role_name = self.cfg.teams.role
        role = self.cfg.roles[role_name]
        messages = [Message(role="system", content=prompt_text(role.prompt))]
        messages += [Message(role=h["role"], content=h["content"]) for h in history]  # type: ignore[arg-type]
        messages.append(Message(role="user", content=prompt + summary_prompt))
        res = await self.engine.generate(
            GenRequest(
                role=role_name,
                messages=messages,
                sampling=role.sampling,
                max_tokens=role.max_tokens,
                stop=list(role.stop),
                thinking=role.thinking,
            )
        )
        output_text = res.text
        step = StepRecord(
            team_id=team_id,
            step_num=len(history) + 1,
            u_t=ToolCall("", "generate", prompt),
            o_t=output_text,
            ok=True,
            assistant_text=output_text,
        )
        step_summary = await self.summarizer.summarize(prompt, step)
        ctx = AdmissionContext(
            query=prompt,
            memory_keys=[k for _, k in summaries],
            u_t=step.u_t,
            s_t=step_summary,
            o_t=output_text,
            team_id=team_id,
            step_num=step.step_num,
        )
        if (await self.controller.decide(ctx)).admit:
            self.memory_bank.add_entry(
                MemoryEntry(
                    id=f"mem-{team_id}-{len(summaries) + 1}",
                    team_id=team_id,
                    step_num=len(history),
                    summary_key=step_summary,
                    raw_value=output_text,
                )
            )
        return output_text

    def finalize_rewards(self, episode_reward: dict[str, float]) -> None:
        """Attach outcome rewards to the admission log (M5 training signal)."""
        self.lts_log.finalize(episode_reward)

    async def execute_task(
        self, query_text: str, *, repo: Path, test_cmd: str | None = None, images: Sequence[Path] = ()
    ) -> RunReport:
        del images  # text-only topology; the studio uses them
        t0 = time.monotonic()
        k = self.cfg.teams.k
        self.query = query_text
        if self.cfg.lts.enabled and self.lts is None:
            self.lts = LTSObserver(
                bank=self.memory_bank,
                summarizer=self.summarizer,
                controller=self.controller,
                log=self.lts_log,
                sink=self.sink,
                run_id=self.run_dir.run_id,
                query=query_text,
                max_keys=self.cfg.lts.max_keys,
                inner=self.observer,
                max_value_chars=self.cfg.lts.max_value_chars,
                background=self.cfg.lts.background_admission,
                admission_concurrency=self.cfg.lts.admission_concurrency,
                ranking=self.cfg.lts.ranking,
                inject_own=self.cfg.lts.inject_own,
                inline_top=self.cfg.lts.inline_top,
                inline_chars=self.cfg.lts.inline_chars,
            )
        self.sink.emit(
            "run_start", goal=query_text, k=k, repo=str(repo), orchestrator=self.cfg.teams.orchestrator
        )
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
        teams = [f"team-{i + 1}" for i in range(k)]
        self.hal_observer = HALObserver(self.hal, self.scheduler, self.sink, self.lts or self.observer, teams)
        results = await self._run_teams(teams, query_text, repo, test_cmd)
        if self.lts is not None:
            await self.lts.drain()  # admissions still in flight land before the run is reported
        report = RunReport(
            self.run_dir.run_id, self.run_dir, query_text, list(results), (time.monotonic() - t0) * 1000.0
        )
        outcomes = [await self._outcome(r, test_cmd) for r in results]
        report.tests = {o.result.team_id: o.tests_passed for o in outcomes}
        agg = await self.aggregator.aggregate(query_text, outcomes)
        report.winner, report.final_answer, report.scores = agg.winner, agg.final_answer, agg.scores
        report.winning_branch = f"airlie/{self.run_dir.run_id}/{agg.winner}"
        self.sink.emit(
            "aggregate", winner=agg.winner, reason=agg.reason, scores=agg.scores, all_failed=agg.all_failed
        )
        self._write_report(report, agg.reason)
        self.finalize_rewards({o.result.team_id: (1.0 if o.tests_passed else 0.0) for o in outcomes})
        m = self.scheduler.metrics()
        self.sink.emit(
            "run_done",
            ok=report.any_ok,
            wall_ms=round(report.wall_ms, 1),
            yields=m.yield_count,
            max_concurrent=m.max_concurrent,
            slot_wait_ms=round(m.slot_wait_ms_total, 1),
            hal_budgets=self.hal.allocate_budgets(teams),
            memory_entries=len(self.memory_bank),
            cross_team_retrievals=sum(e.access_count for e in self.memory_bank.entries()),
        )
        return report

    async def _run_teams(
        self, teams: list[str], goal: str, repo: Path, test_cmd: str | None
    ) -> list[TeamResult]:
        """K teams in parallel; with `teams.stop_on_first_pass`, the first team whose tests pass cancels the
        rest."""
        tasks = {tid: asyncio.create_task(self._run_team(tid, goal, repo, test_cmd)) for tid in teams}
        results: dict[str, TeamResult] = {}
        pending = set(tasks.values())
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.cancelled():
                    continue  # recorded below as "cancelled"
                r = t.result()
                results[r.team_id] = r
                early = self.cfg.teams.stop_on_first_pass and test_cmd and r.ok and pending
                if early and (await self._outcome(r, test_cmd)).tests_passed:
                    self.sink.emit(
                        "early_stop", winner=r.team_id, cancelled=[x for x in teams if x not in results]
                    )
                    for other in pending:
                        other.cancel()
        for tid, task in tasks.items():
            if tid not in results:
                try:
                    results[tid] = await task
                except asyncio.CancelledError:
                    results[tid] = TeamResult(
                        tid, False, "cancelled", "cancelled: another team passed first", 0, 0, 0, 0.0
                    )
                    self.sink.emit("team_done", team_id=tid, ok=False, stop_reason="cancelled", steps=0)
        return [results[tid] for tid in teams]

    def _budget_provider(self, team_id: str) -> Callable[[], int | None]:
        def provide() -> int | None:
            return self.hal_observer.budget_for(team_id) if self.hal_observer else None

        return provide

    async def _outcome(self, r: TeamResult, test_cmd: str | None) -> TeamOutcome:
        wt = self.run_dir.path / r.team_id
        if not test_cmd:
            return TeamOutcome(r, wt, None, "")
        # the test command loads the code the team wrote: it runs as the run_tests tool runs it
        proc = await started(
            asyncio.create_subprocess_shell(
                test_cmd,
                cwd=wt,
                env=agent_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # its own process group: kill_group reaches what it starts
            )
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.tools.shell_timeout_s)
        except TimeoutError:
            await kill_group(proc)
            return TeamOutcome(r, wt, False, "test command timed out")
        except BaseException:  # cancelled: nothing the command started may outlive its caller
            await kill_group(proc)
            raise
        return TeamOutcome(r, wt, proc.returncode == 0, out.decode("utf-8", "replace")[-4000:])

    def _write_report(self, report: RunReport, reason: str) -> None:
        write_report(report, reason, self.run_dir.path / "report.md")

    async def _run_team(self, team_id: str, goal: str, repo: Path, test_cmd: str | None) -> TeamResult:
        wt = add_worktree(repo, self.run_dir.path / team_id, branch=f"airlie/{self.run_dir.run_id}/{team_id}")
        role_name = self.cfg.teams.role
        role = self.cfg.roles[role_name]
        tools = ToolRegistry.builtin(
            self.cfg.tools.builtin,
            ToolContext(
                wt,
                self.cfg.tools.shell_timeout_s,
                self.cfg.tools.output_cap_chars,
                test_cmd,
                write_file_max_overwrite_lines=self.cfg.tools.write_file_max_overwrite_lines,
                **self.cfg.tools.confinement(),
            ),
        )
        observer: StepObserver = self.hal_observer or self.observer
        if self.lts is not None:
            tools.add(RetrieveValue(self.memory_bank, team_id, on_retrieve=self.lts.on_retrieve))
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
            draft_budget=self._budget_provider(team_id),
            preamble=self.preamble,
            emit=self.sink.emit,
        )
        self.sink.emit("team_start", team_id=team_id, worktree=str(wt))
        result = await self.team.run(ctx, observer)
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
