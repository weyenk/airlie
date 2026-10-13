"""LTSObserver: the Learning-to-Share substrate attached to every team step (spec 01 §4 steps 5–7, 04 §3).

before_step → inject the keys ranked for this team (own keys excluded, top values inlined);
after_step → summarize (s_t), decide with C_θ, admit — in the background by default, so admission never
sits on a team's critical path. `drain()` waits for the in-flight decisions; the orchestrator calls it once
the teams are done."""

from __future__ import annotations

import asyncio

from airlie.lts.bank import MemoryEntry, SharedMemoryBank
from airlie.lts.controller.protocol import AdmissionContext, LTSAdmissionController
from airlie.lts.inject import Ranking, rank_entries, render_entries
from airlie.lts.log import AdmissionLog
from airlie.lts.summarizer import Summarizer
from airlie.obs import EventSink
from airlie.teams.protocol import StepObserver, StepRecord


class LTSObserver:
    def __init__(
        self,
        *,
        bank: SharedMemoryBank,
        summarizer: Summarizer,
        controller: LTSAdmissionController,
        log: AdmissionLog,
        sink: EventSink,
        run_id: str,
        query: str,
        max_keys: int,
        inner: StepObserver,
        max_value_chars: int = 20000,
        background: bool = True,
        admission_concurrency: int = 2,
        ranking: Ranking = "relevance",
        inject_own: bool = False,
        inline_top: int = 2,
        inline_chars: int = 1500,
        context_chars: int = 2000,
    ) -> None:
        self.bank = bank
        self.summarizer = summarizer
        self.controller = controller
        self.log = log
        self.sink = sink
        self.run_id = run_id
        self.query = query
        self.max_keys = max_keys
        self.inner = inner
        self.max_value_chars = max_value_chars
        self.background = background
        self.ranking: Ranking = ranking
        self.inject_own = inject_own
        self.inline_top = inline_top
        self.inline_chars = inline_chars
        self.context_chars = context_chars
        self._sem = asyncio.Semaphore(max(1, admission_concurrency))
        self._pending: set[asyncio.Task[None]] = set()
        self._last_context: dict[str, str] = {}
        self._inlined: dict[str, set[str]] = {}

    # ---- injection (spec 01 §4 step 7) ----
    async def before_step(self, team_id: str, step_num: int) -> str | None:
        parts: list[str] = []
        inner = await self.inner.before_step(team_id, step_num)
        if inner:
            parts.append(inner)
        context = self.query + "\n" + self._last_context.get(team_id, "")
        ranked = rank_entries(
            self.bank.entries(),
            context=context,
            max_keys=self.max_keys,
            exclude_team=None if self.inject_own else team_id,
            ranking=self.ranking,
        )
        seen = self._inlined.setdefault(team_id, set())
        fresh = [r for r in ranked if r.entry.id not in seen and (self.ranking == "recency" or r.score > 0)]
        inline_ids = {r.entry.id for r in fresh[: self.inline_top]}
        seen |= inline_ids
        keys = render_entries(ranked, inline_ids=inline_ids, inline_chars=self.inline_chars)
        if keys:
            parts.append(keys)
        return "\n".join(parts) if parts else None

    # ---- admission (spec 01 §4 steps 5–6) ----
    async def after_step(self, step: StepRecord) -> None:
        await self.inner.after_step(step)
        self._last_context[step.team_id] = (
            f"{step.assistant_text}\n{step.u_t.name} {step.u_t.arguments}\n{step.o_t}"[-self.context_chars :]
        )
        if not self.background:
            await self._admit(step)
            return
        task = asyncio.create_task(self._admit(step), name=f"lts-admit-{step.team_id}-{step.step_num}")
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Wait for every in-flight admission; safe to call repeatedly."""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _admit(self, step: StepRecord) -> None:
        async with self._sem:
            try:
                await self._summarize_and_decide(step)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a broken summarizer or controller must not take a team down
                err = f"{type(exc).__name__}: {exc}"
                self.sink.emit("admission_error", team_id=step.team_id, step=step.step_num, error=err)

    async def _summarize_and_decide(self, step: StepRecord) -> None:
        memory_keys = [k for _, k in self.bank.get_summaries()[-self.max_keys :]]
        s_t = await self.summarizer.summarize(self.query, step)
        ctx = AdmissionContext(
            query=self.query,
            memory_keys=memory_keys,
            u_t=step.u_t,
            s_t=s_t,
            o_t=step.o_t[: self.max_value_chars],
            team_id=step.team_id,
            step_num=step.step_num,
            ok=step.ok,
        )
        decision = await self.controller.decide(ctx)
        entry_id: str | None = None
        if decision.admit:
            entry_id = self.bank.new_id(step.team_id, step.step_num)
            self.bank.add_entry(
                MemoryEntry(
                    id=entry_id,
                    team_id=step.team_id,
                    step_num=step.step_num,
                    summary_key=s_t,
                    raw_value=step.o_t[: self.max_value_chars],
                )
            )
        self.log.admission(
            run_id=self.run_id,
            team_id=step.team_id,
            step=step.step_num,
            query=self.query,
            memory_keys=memory_keys,
            u_t=step.u_t,
            s_t=s_t,
            o_t=step.o_t[: self.max_value_chars],
            decision=decision.admit,
            p_yes=decision.p_yes,
            reason=decision.reason,
            entry_id=entry_id,
            controller=self.controller.kind,
        )
        self.sink.emit(
            "admission",
            team_id=step.team_id,
            step=step.step_num,
            admit=decision.admit,
            p_yes=round(decision.p_yes, 3),
            reason=decision.reason,
            entry_id=entry_id,
            summary=s_t,
        )

    def on_retrieve(self, team_id: str, entry_id: str, found: bool) -> None:
        self.log.retrieval(run_id=self.run_id, team_id=team_id, step=-1, entry_id=entry_id, found=found)
        self.sink.emit("retrieval", team_id=team_id, entry_id=entry_id, found=found)
