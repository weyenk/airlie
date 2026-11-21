"""Router: the decision policy, the chief router and the JSONL routing log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from airlie.router import ChiefRouter, RoutingLog, policy, ranked

CRITERIA = {
    "architect": "Implement or change code",
    "qa": "Tests and edge cases",
    "docs": "README and guides",
}


def test_ranked_orders_by_probability_then_name() -> None:
    assert ranked({"qa": 0.3, "coder": 0.6, "docs": 0.1}) == [("architect", 0.6), ("qa", 0.3), ("docs", 0.1)]
    assert ranked({"b": 0.5, "a": 0.5}) == [("a", 0.5), ("b", 0.5)]


def test_policy_matches_ai_mode() -> None:
    assert policy([("architect", 0.8), ("qa", 0.2)], 0.9, ambiguous_gap=0.15, min_confidence=0.5) == "clear"
    assert (
        policy([("architect", 0.5), ("qa", 0.4)], 0.9, ambiguous_gap=0.15, min_confidence=0.5) == "ambiguous"
    )
    assert (
        policy([("architect", 0.8), ("qa", 0.2)], 0.3, ambiguous_gap=0.15, min_confidence=0.5)
        == "low-confidence"
    )


async def test_chief_router_uses_the_hint_and_rejects_unknown_roles() -> None:
    d = await ChiefRouter().route("x", CRITERIA, hint="qa")
    assert d.decision == "hint" and d.chosen == "qa" and d.confidence == 1.0
    with pytest.raises(ValueError, match="'ux'"):
        await ChiefRouter().route("x", CRITERIA, hint="ux")


async def test_routing_log_writes_one_json_line_per_decision(tmp_path: Path) -> None:
    log = RoutingLog(tmp_path / "routing.jsonl")
    d = await ChiefRouter().route("x", CRITERIA, hint="qa")
    log.record("fix", d)
    rec = json.loads((tmp_path / "routing.jsonl").read_text().splitlines()[0])
    assert rec["subtask"] == "fix" and rec["chosen"] == "qa" and rec["decision"] == "hint"


def test_policy_thresholds_are_strict() -> None:
    # gap exactly at the threshold is not ambiguous; confidence exactly at the minimum is not low
    assert policy([("a", 0.75), ("b", 0.5)], 0.9, ambiguous_gap=0.25, min_confidence=0.5) == "clear"
    assert policy([("a", 0.75), ("b", 0.25)], 0.5, ambiguous_gap=0.25, min_confidence=0.5) == "clear"
    assert policy([("a", 0.75)], 0.1, ambiguous_gap=0.25, min_confidence=0.5) == "low-confidence"
    assert policy([("a", 0.5), ("b", 0.5)], 0.1, ambiguous_gap=0.25, min_confidence=0.5) == "ambiguous"
