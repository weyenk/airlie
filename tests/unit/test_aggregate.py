"""Aggregators (spec 01 §4 step 9): pick the winning team and write the final answer."""

from __future__ import annotations

from pathlib import Path

from airlie.engine import FakeEngine, FakeReply
from airlie.teams import TeamResult
from airlie.teams.aggregate import LLMAggregator, TeamOutcome, TestScoreAggregator


def outcome(team: str, ok: bool, passed: bool | None, steps: int = 5) -> TeamOutcome:
    return TeamOutcome(
        result=TeamResult(
            team, ok, "done" if ok else "max_steps", f"{team} says done", steps, 100, 50, 1000.0
        ),
        worktree=Path(f"/wt/{team}"),
        tests_passed=passed,
        test_output="3 passed" if passed else "1 failed",
    )


async def test_test_score_prefers_passing_then_fewest_steps() -> None:
    agg = TestScoreAggregator()
    res = await agg.aggregate(
        "goal",
        [
            outcome("team-1", True, False, 3),
            outcome("team-2", True, True, 9),
            outcome("team-3", True, True, 4),
        ],
    )
    assert res.winner == "team-3"
    assert res.scores == {"team-1": 0.0, "team-2": 1.0, "team-3": 1.0}
    assert "team-3" in res.final_answer


async def test_test_score_with_no_passing_team_names_closest() -> None:
    res = await TestScoreAggregator().aggregate(
        "goal", [outcome("team-1", False, False), outcome("team-2", True, False)]
    )
    assert res.winner == "team-2" and res.all_failed is True


async def test_llm_aggregator_parses_winner_and_answer() -> None:
    engine = FakeEngine(
        {
            "aggregator": [
                FakeReply(text="WINNER: team-2\n\nTeam 2 passes all tests with the smallest change.")
            ]
        }
    )
    agg = LLMAggregator(engine, role="aggregator")
    res = await agg.aggregate("goal", [outcome("team-1", True, True), outcome("team-2", True, True)])
    assert res.winner == "team-2"
    assert res.final_answer.startswith("Team 2 passes")
    prompt = engine.requests[0].messages[-1].content
    assert "team-1" in prompt and "3 passed" in prompt and "goal" in prompt


async def test_llm_aggregator_falls_back_when_no_valid_winner() -> None:
    engine = FakeEngine({"aggregator": [FakeReply(text="I think team-9 is best.")]})
    res = await LLMAggregator(engine).aggregate(
        "goal", [outcome("team-1", True, False), outcome("team-2", True, True)]
    )
    assert res.winner == "team-2" and "fallback" in res.reason
