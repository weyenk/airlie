"""route:eval scores a labelled routing set the way ai-mode's classify --eval did."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.bench.route_eval import evaluate, load_cases, load_criteria, render
from airlie.router.protocol import RouteDecision

ROOT = Path(__file__).resolve().parents[2]


class ScriptedRouter:
    def __init__(self, answers: dict[str, tuple[str, str, float]]) -> None:
        self.answers = answers  # task -> (chosen, decision, confidence)

    async def route(self, brief, criteria, hint=None, *, question="role", instructions=""):  # type: ignore[no-untyped-def]
        chosen, decision, conf = self.answers[brief]
        return RouteDecision(question, chosen, chosen, decision, {chosen: conf}, conf, hint)


def test_seed_files_parse_and_cover_every_role() -> None:
    cases = load_cases(ROOT / "evals/routing.jsonl")
    criteria = load_criteria(ROOT / "evals/routing-roles.yaml")
    assert len(cases) == 54 and {c.expect for c in cases} == set(criteria)


async def test_evaluate_reports_accuracy_confidence_wrong_but_clear_and_confusions(tmp_path: Path) -> None:
    p = tmp_path / "cases.jsonl"
    rows = [
        {"question": "role", "task": "write tests", "expect": "qa"},
        {"question": "role", "task": "fix the bug", "expect": "coder"},
        {"question": "role", "task": "write the README", "expect": "docs"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    router = ScriptedRouter(
        {
            "write tests": ("qa", "clear", 0.9),
            "fix the bug": ("qa", "clear", 0.8),
            "write the README": ("coder", "ambiguous", 0.4),
        }
    )
    rep = await evaluate(router, load_cases(p), {"qa": "t", "coder": "c", "docs": "d"})
    assert rep.n == 3 and rep.correct == 1 and abs(rep.accuracy - 1 / 3) < 1e-9
    assert abs(rep.mean_confidence - 0.7) < 1e-9
    assert rep.wrong_but_clear == 1
    assert rep.confusions == {"coder->qa": 1, "docs->coder": 1}
    text = render(rep)
    assert "accuracy 0.33" in text and "wrong-but-clear 1" in text and "docs->coder" in text
