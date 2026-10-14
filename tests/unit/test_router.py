"""Router: Kev (/v1/systemone) client, ai-mode's decision policy, chief fallback, JSONL log."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from airlie.router import ChiefRouter, JevClient, JevRouter, RoutingLog, policy, ranked

CRITERIA = {"coder": "Implement or change code", "qa": "Tests and edge cases", "docs": "README and guides"}


def systemone(answers: dict[str, float], confidence: float, choice: str | None = None):  # type: ignore[no-untyped-def]
    """A MockTransport handler answering /v1/systemone with the given probabilities."""
    seen: list[dict] = []  # type: ignore[type-arg]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/systemone"
        body = json.loads(request.content)
        seen.append(body)
        q = next(iter(body["questions"]))
        top = choice or max(answers, key=lambda k: answers[k])
        return httpx.Response(
            200,
            json={
                "answers": {q: {"choice": top, "probabilities": answers, "confidence": confidence}},
                "usage": {"input_tokens": 42},
            },
        )

    return handler, seen


def jev(handler, **kw) -> JevRouter:  # type: ignore[no-untyped-def]
    client = JevClient("http://k/v1", transport=httpx.MockTransport(handler))
    return JevRouter(client, ambiguous_gap=0.15, min_confidence=0.5, **kw)


def test_ranked_orders_by_probability_then_name() -> None:
    assert ranked({"qa": 0.3, "coder": 0.6, "docs": 0.1}) == [("coder", 0.6), ("qa", 0.3), ("docs", 0.1)]
    assert ranked({"b": 0.5, "a": 0.5}) == [("a", 0.5), ("b", 0.5)]


def test_policy_matches_ai_mode() -> None:
    assert policy([("coder", 0.8), ("qa", 0.2)], 0.9, ambiguous_gap=0.15, min_confidence=0.5) == "clear"
    assert policy([("coder", 0.5), ("qa", 0.4)], 0.9, ambiguous_gap=0.15, min_confidence=0.5) == "ambiguous"
    assert (
        policy([("coder", 0.8), ("qa", 0.2)], 0.3, ambiguous_gap=0.15, min_confidence=0.5) == "low-confidence"
    )


async def test_client_posts_the_system_one_request_shape() -> None:
    handler, seen = systemone({"coder": 0.9, "qa": 0.1}, 0.95)
    client = JevClient("http://kev.test/v1", model="kev", transport=httpx.MockTransport(handler))
    ans = await client.ask("fix the 500", "role", "Which specialist should handle this task?", CRITERIA)
    assert seen[0] == {
        "model": "kev",
        "state": "fix the 500",
        "questions": {
            "role": {
                "type": "choice",
                "instructions": "Which specialist should handle this task?",
                "criteria": CRITERIA,
            }
        },
    }
    assert ans.choice == "coder" and ans.confidence == 0.95 and ans.input_tokens == 42


async def test_clear_decision_takes_the_choice_over_the_hint() -> None:
    handler, _ = systemone({"coder": 0.9, "qa": 0.1}, 0.95)
    d = await jev(handler).route("fix the 500", CRITERIA, hint="docs")
    assert d.decision == "clear" and d.chosen == "coder" and d.hint == "docs"


async def test_ambiguous_takes_the_hint_when_it_is_in_the_top_two() -> None:
    handler, _ = systemone({"coder": 0.45, "qa": 0.4, "docs": 0.15}, 0.9)
    r = jev(handler)
    assert (await r.route("add tests for the fix", CRITERIA, hint="qa")).chosen == "qa"
    assert (await r.route("add tests for the fix", CRITERIA, hint="docs")).chosen == "coder"
    assert (await r.route("add tests for the fix", CRITERIA)).chosen == "coder"


async def test_low_confidence_takes_the_hint_else_top() -> None:
    handler, _ = systemone({"coder": 0.8, "qa": 0.2}, 0.2)
    r = jev(handler)
    assert (await r.route("x", CRITERIA, hint="docs")).chosen == "docs"
    assert (await r.route("x", CRITERIA)).chosen == "coder"


async def test_choice_outside_closed_set_falls_back() -> None:
    handler, _ = systemone({"coder": 0.6, "qa": 0.4}, 0.9, choice="chief")
    r = jev(handler)
    d = await r.route("x", CRITERIA, hint="qa")
    assert d.decision == "invalid" and d.chosen == "qa"
    assert (await r.route("x", CRITERIA)).chosen == "coder"  # top in-set probability


async def test_transport_error_is_a_decision_not_an_exception() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    r = jev(boom)
    d = await r.route("x", CRITERIA, hint="qa")
    assert d.decision == "error" and d.chosen == "qa"
    with pytest.raises(ValueError, match="no hint"):
        await r.route("x", CRITERIA)


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
