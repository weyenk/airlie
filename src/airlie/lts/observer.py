"""LTSObserver: the Learning-to-Share substrate attached to every team step (spec 01 §4 steps 5–7, 04 §3).

before_step → inject the current summary keys; after_step → summarize (s_t), decide with C_θ, admit (s_t, o_t)."""

from __future__ import annotations

from airlie.lts.bank import MemoryEntry, SharedMemoryBank
from airlie.lts.controller.protocol import AdmissionContext, LTSAdmissionController
from airlie.lts.inject import render_keys
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

    async def before_step(self, team_id: str, step_num: int) -> str | None:
        parts: list[str] = []
        inner = await self.inner.before_step(team_id, step_num)
        if inner:
            parts.append(inner)
        keys = render_keys(self.bank.get_summaries(), max_keys=self.max_keys)
        if keys:
            parts.append(keys)
        return "\n".join(parts) if parts else None

    async def after_step(self, step: StepRecord) -> None:
        await self.inner.after_step(step)
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
