"""Summary keys s_t. `truncate` is the spec 04 §3 blueprint (`output[:n]`); `role` asks a small model for the
15–20 word key the LTS paper uses, falling back to truncation on engine errors."""

from __future__ import annotations

from typing import Protocol

from airlie.config import Sampling, SummarizerSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import EngineError, GenRequest, Message
from airlie.teams.protocol import StepRecord


class Summarizer(Protocol):
    kind: str

    async def summarize(self, query: str, step: StepRecord) -> str: ...


class TruncateSummarizer:
    kind = "truncate"

    def __init__(self, chars: int = 100) -> None:
        self.chars = chars

    async def summarize(self, query: str, step: StepRecord) -> str:
        return f"Team {step.team_id} step output summary: {step.o_t[: self.chars]}"


PROMPT = """Task: {query}

An agent took this step:
- action: {tool}({args})
- output:
{output}

Write one line of at most {max_words} words describing the step's purpose and outcome, so another team can
decide whether to read the full output. No preamble, no quotes."""


class RoleSummarizer:
    kind = "role"

    def __init__(
        self, engine: Engine, role: str = "summarizer", max_words: int = 20, fallback_chars: int = 100
    ):
        self.engine = engine
        self.role = role
        self.max_words = max_words
        self._fallback = TruncateSummarizer(fallback_chars)

    async def summarize(self, query: str, step: StepRecord) -> str:
        text = PROMPT.format(
            query=query,
            tool=step.u_t.name,
            args=step.u_t.arguments[:300],
            output=step.o_t[:3000],
            max_words=self.max_words,
        )
        req = GenRequest(
            role=self.role,
            messages=[Message(role="user", content=text)],
            sampling=Sampling(temperature=0.2, top_p=0.9),
            max_tokens=64,
            stop=["\n"],
            thinking=False,
        )
        try:
            res = await self.engine.generate(req)
        except EngineError:
            return await self._fallback.summarize(query, step)
        line = res.text.strip().splitlines()[0].strip().strip('"') if res.text.strip() else ""
        if not line:
            return await self._fallback.summarize(query, step)
        return " ".join(line.split()[: self.max_words * 2])


def build_summarizer(spec: SummarizerSpec, engine: Engine | None = None) -> Summarizer:
    if spec.kind == "truncate":
        return TruncateSummarizer(spec.truncate_chars)
    if engine is None:
        raise ValueError("lts.summarizer.kind=role needs an engine")
    return RoleSummarizer(engine, spec.role, spec.max_words, spec.truncate_chars)
