"""Aggregation (spec 01 §4 step 9): "the LLM Aggregator combines team outputs into the final verified answer".

`TestScoreAggregator` picks by test outcome then fewest steps; `LLMAggregator` lets the `aggregator` role read
every team's final text and test output, name the winner and write the answer, falling back to the test score
when it names no valid team. Code is never merged across teams: one branch wins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from airlie.config import Sampling
from airlie.engine.protocol import Engine
from airlie.engine.types import EngineError, GenRequest, Message
from airlie.teams.protocol import TeamResult


@dataclass
class TeamOutcome:
    result: TeamResult
    worktree: Path
    tests_passed: bool | None
    test_output: str = ""


@dataclass
class AggregateResult:
    winner: str
    final_answer: str
    scores: dict[str, float]
    reason: str
    all_failed: bool = False


class Aggregator(Protocol):
    kind: str

    async def aggregate(self, goal: str, outcomes: list[TeamOutcome]) -> AggregateResult: ...


def _score(o: TeamOutcome) -> float:
    if o.tests_passed is True:
        return 1.0
    if o.tests_passed is None:
        return 0.5 if o.result.ok else 0.0
    return 0.0


def _rank(outcomes: list[TeamOutcome]) -> list[TeamOutcome]:
    return sorted(
        outcomes, key=lambda o: (-_score(o), 0 if o.result.ok else 1, o.result.steps, o.result.team_id)
    )


class TestScoreAggregator:
    kind = "test_score"

    async def aggregate(self, goal: str, outcomes: list[TeamOutcome]) -> AggregateResult:
        ranked = _rank(outcomes)
        best = ranked[0]
        scores = {o.result.team_id: _score(o) for o in outcomes}
        all_failed = all(s < 1.0 for s in scores.values())
        if all_failed:
            answer = (
                f"No team passed the tests. Closest: {best.result.team_id} ({best.result.stop_reason}, "
                f"{best.result.steps} steps). Its summary: {best.result.final_text[:500]}"
            )
        else:
            answer = (
                f"{best.result.team_id} passes the tests in {best.result.steps} steps. "
                f"Its summary: {best.result.final_text[:800]}"
            )
        return AggregateResult(best.result.team_id, answer, scores, "test_score", all_failed)


PROMPT = """Goal:
{goal}

{teams}

Decide which single team's branch is the final answer. Prefer a team whose tests pass; among those, the
smallest, clearest change that fully meets the goal. If none pass, name the closest team and say what is
missing. Reply with a first line `WINNER: <team id>` and then the final answer in at most five sentences."""


class LLMAggregator:
    kind = "llm"

    def __init__(self, engine: Engine, role: str = "aggregator", fallback: Aggregator | None = None) -> None:
        self.engine = engine
        self.role = role
        self.fallback = fallback or TestScoreAggregator()

    async def aggregate(self, goal: str, outcomes: list[TeamOutcome]) -> AggregateResult:
        blocks = []
        for o in outcomes:
            verdict = {True: "PASS", False: "FAIL", None: "not run"}[o.tests_passed]
            blocks.append(
                f"## {o.result.team_id} (tests: {verdict}; stop: {o.result.stop_reason}; "
                f"steps: {o.result.steps})\n"
                f"Final report:\n{o.result.final_text[:1500]}\n\nTest output (tail):\n{o.test_output[-800:]}"
            )
        prompt = PROMPT.format(goal=goal, teams="\n\n".join(blocks))
        req = GenRequest(
            role=self.role,
            messages=[Message(role="user", content=prompt)],
            sampling=Sampling(temperature=0.2, top_p=0.9),
            max_tokens=600,
            stop=[],
            thinking=False,
        )
        ids = {o.result.team_id for o in outcomes}
        try:
            res = await self.engine.generate(req)
        except EngineError as e:
            fb = await self.fallback.aggregate(goal, outcomes)
            return AggregateResult(
                fb.winner, fb.final_answer, fb.scores, f"fallback: engine error {e}", fb.all_failed
            )
        m = re.search(r"WINNER:\s*([\w-]+)", res.text)
        scores = {o.result.team_id: _score(o) for o in outcomes}
        if m and m.group(1) in ids:
            answer = res.text[m.end() :].strip() or res.text.strip()
            return AggregateResult(m.group(1), answer, scores, "llm", all(s < 1.0 for s in scores.values()))
        fb = await self.fallback.aggregate(goal, outcomes)
        return AggregateResult(
            fb.winner, fb.final_answer, fb.scores, "fallback: no valid WINNER line", fb.all_failed
        )
