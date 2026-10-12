"""task bench:lts — memory-free baseline vs LTS on graded tasks; verdict on the spec 04 §4 ≥25 % criterion."""

from __future__ import annotations

from airlie.bench.lts_bench import BenchRow, summarize, verdict


def row(controller: str, wall_s: float, passed: bool, task: str = "t1", rep: int = 0) -> BenchRow:
    return BenchRow(
        task=task,
        controller=controller,
        repeat=rep,
        wall_s=wall_s,
        steps=10,
        prompt_tokens=100,
        completion_tokens=50,
        admissions=3 if controller != "never" else 0,
        retrievals=1 if controller != "never" else 0,
        teams_passed=1 if passed else 0,
        teams=3,
        run_id="r",
    )


def test_summary_averages_per_controller() -> None:
    rows = [
        row("never", 100, True),
        row("never", 120, False, rep=1),
        row("heuristic", 70, True),
        row("heuristic", 80, True, rep=1),
    ]
    s = summarize(rows)
    assert s["never"]["mean_wall_s"] == 110.0 and s["never"]["pass_rate"] == 0.5
    assert s["heuristic"]["mean_wall_s"] == 75.0 and s["heuristic"]["pass_rate"] == 1.0
    assert s["heuristic"]["mean_admissions"] == 3.0


def test_verdict_against_25_percent() -> None:
    s = summarize([row("never", 100, True), row("heuristic", 70, True)])
    v = verdict(s, baseline="never", candidate="heuristic", target_pct=25.0)
    assert v.reduction_pct == 30.0 and v.met is True
    s2 = summarize([row("never", 100, True), row("heuristic", 90, True)])
    v2 = verdict(s2, baseline="never", candidate="heuristic", target_pct=25.0)
    assert v2.reduction_pct == 10.0 and v2.met is False and "10.0" in v2.text


def test_verdict_notes_pass_rate_regression() -> None:
    s = summarize([row("never", 100, True), row("heuristic", 50, False)])
    v = verdict(s, baseline="never", candidate="heuristic", target_pct=25.0)
    assert v.met is False and "pass rate" in v.text
