"""`task bench:lts`: the spec 04 §4 integration benchmark on our graded tasks.

Runs each task with the memory-free baseline (`lts.controller.kind: never`) and with the configured
controller, K teams each, and reports wall-clock, steps, tokens, admissions, retrievals and hidden-test pass
rate. The verdict applies the spec's criterion: ≥25 % wall-clock reduction vs the baseline without losing pass
rate."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class BenchRow:
    task: str
    controller: str
    repeat: int
    wall_s: float
    steps: int
    prompt_tokens: int
    completion_tokens: int
    admissions: int
    retrievals: int
    teams_passed: int
    teams: int
    run_id: str


@dataclass
class Verdict:
    reduction_pct: float
    met: bool
    text: str


def summarize(rows: list[BenchRow]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    by: dict[str, list[BenchRow]] = {}
    for r in rows:
        by.setdefault(r.controller, []).append(r)
    for ctrl, rs in by.items():
        n = len(rs)
        out[ctrl] = {
            "runs": float(n),
            "mean_wall_s": round(sum(r.wall_s for r in rs) / n, 1),
            "mean_steps": round(sum(r.steps for r in rs) / n, 1),
            "mean_admissions": round(sum(r.admissions for r in rs) / n, 1),
            "mean_retrievals": round(sum(r.retrievals for r in rs) / n, 1),
            "pass_rate": round(sum(1 for r in rs if r.teams_passed > 0) / n, 3),
            "team_pass_rate": round(sum(r.teams_passed for r in rs) / max(sum(r.teams for r in rs), 1), 3),
        }
    return out


def verdict(
    summary: dict[str, dict[str, float]], *, baseline: str, candidate: str, target_pct: float
) -> Verdict:
    b, c = summary.get(baseline), summary.get(candidate)
    if not b or not c:
        return Verdict(0.0, False, f"missing rows for {baseline!r} or {candidate!r}")
    red = (
        round((b["mean_wall_s"] - c["mean_wall_s"]) / b["mean_wall_s"] * 100.0, 1)
        if b["mean_wall_s"]
        else 0.0
    )
    met = red >= target_pct and c["pass_rate"] >= b["pass_rate"]
    text = (
        f"wall-clock {b['mean_wall_s']:.1f}s → {c['mean_wall_s']:.1f}s "
        f"({red:+.1f} % reduction; target ≥ {target_pct:.0f} %); "
        f"pass rate {b['pass_rate']:.2f} → {c['pass_rate']:.2f}"
    )
    if c["pass_rate"] < b["pass_rate"]:
        text += "; pass rate regressed"
    text += "; criterion " + ("MET" if met else "NOT met")
    return Verdict(red, met, text)


def save(rows: list[BenchRow], summary: dict[str, dict[str, float]], v: Verdict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"rows": [asdict(r) for r in rows], "summary": summary, "verdict": asdict(v)}, indent=1)
    )
    return path


def render(summary: dict[str, dict[str, float]], v: Verdict) -> str:
    cols = [
        "controller",
        "runs",
        "mean_wall_s",
        "mean_steps",
        "mean_admissions",
        "mean_retrievals",
        "pass_rate",
        "team_pass_rate",
    ]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for ctrl, s in summary.items():
        lines.append("| " + " | ".join([ctrl] + [f"{s[c]:g}" for c in cols[1:]]) + " |")
    lines.append("")
    lines.append(v.text)
    return "\n".join(lines)


def events_counts(events_path: Path) -> dict[str, Any]:
    steps = admissions = retrievals = 0
    for line in events_path.read_text().splitlines():
        e = json.loads(line)
        if e["kind"] == "step":
            steps += 1
        elif e["kind"] == "admission" and e.get("admit"):
            admissions += 1
        elif e["kind"] == "retrieval" and e.get("found"):
            retrievals += 1
    return {"steps": steps, "admissions": admissions, "retrievals": retrievals}
