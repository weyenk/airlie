"""`task bench:topology`: single-agent baseline vs the spec's K teams vs fork-when-stuck on graded tasks.

Verdict (design §9): the candidate must not lower the pass rate and must either raise it or cut mean
wall-clock on solved runs by ≥ target_pct against `single`."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from airlie.bench.lts_bench import Verdict
from airlie.config import AirlieConfig

TOPOLOGIES = ("single", "teams", "fork", "studio")


@dataclass
class TopoRow:
    task: str
    topology: str
    repeat: int
    wall_s: float
    steps: int
    prompt_tokens: int
    completion_tokens: int
    forks: int
    rounds: int
    winner_passed: bool
    any_passed: bool
    branches: int
    run_id: str
    reviews: int = 0
    blocking: int = 0
    fix_rounds: int = 0


def apply_topology(cfg: AirlieConfig, name: str) -> AirlieConfig:
    c = cfg.model_copy(deep=True)
    if name == "single":
        c.topology.kind = "single"
        c.teams.k = 1
        c.lts.enabled = False
    elif name == "teams":
        c.topology.kind = "teams"
    elif name == "fork":
        c.topology.kind = "fork"
    elif name == "studio":
        c.topology.kind = "studio"
    else:
        raise ValueError(f"unknown topology {name!r}; known: {', '.join(TOPOLOGIES)}")
    return c


def summarize(rows: list[TopoRow]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    by: dict[str, list[TopoRow]] = {}
    for r in rows:
        by.setdefault(r.topology, []).append(r)
    for topo, rs in by.items():
        n = len(rs)
        solved = [r for r in rs if r.winner_passed]
        out[topo] = {
            "runs": float(n),
            "mean_wall_s": round(sum(r.wall_s for r in rs) / n, 1),
            "mean_wall_solved_s": round(sum(r.wall_s for r in solved) / len(solved), 1) if solved else 0.0,
            "mean_steps": round(sum(r.steps for r in rs) / n, 1),
            "mean_forks": round(sum(r.forks for r in rs) / n, 2),
            "pass_rate": round(len(solved) / n, 3),
            "any_pass_rate": round(sum(1 for r in rs if r.any_passed) / n, 3),
            "mean_reviews": round(sum(r.reviews for r in rs) / n, 2),
            "mean_fix_rounds": round(sum(r.fix_rounds for r in rs) / n, 2),
        }
    return out


def verdict(
    summary: dict[str, dict[str, float]], *, baseline: str, candidate: str, target_pct: float
) -> Verdict:
    b, c = summary.get(baseline), summary.get(candidate)
    if not b or not c:
        return Verdict(0.0, False, f"missing rows for {baseline!r} or {candidate!r}")
    bw, cw = b["mean_wall_solved_s"], c["mean_wall_solved_s"]
    red = round((bw - cw) / bw * 100.0, 1) if bw else 0.0
    no_regression = c["pass_rate"] >= b["pass_rate"]
    met = no_regression and (c["pass_rate"] > b["pass_rate"] or red >= target_pct)
    text = (
        f"{candidate} vs {baseline}: pass rate {b['pass_rate']:.2f} → {c['pass_rate']:.2f}; "
        f"wall-clock on solved runs {bw:.1f}s → {cw:.1f}s ({red:+.1f} %; target ≥ {target_pct:.0f} %)"
    )
    if not no_regression:
        text += "; pass rate regressed"
    text += "; criterion " + ("MET" if met else "NOT met")
    return Verdict(red, met, text)


def render(summary: dict[str, dict[str, float]], v: Verdict) -> str:
    cols = [
        "topology",
        "runs",
        "mean_wall_s",
        "mean_wall_solved_s",
        "mean_steps",
        "mean_forks",
        "pass_rate",
        "any_pass_rate",
        "mean_reviews",
        "mean_fix_rounds",
    ]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for topo, s in summary.items():
        lines.append("| " + " | ".join([topo] + [f"{s[c]:g}" for c in cols[1:]]) + " |")
    return "\n".join([*lines, "", v.text])


def save(rows: list[TopoRow], summary: dict[str, dict[str, float]], v: Verdict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"rows": [asdict(r) for r in rows], "summary": summary, "verdict": asdict(v)}, indent=1)
    )
    return path


def events_counts(events_path: Path) -> dict[str, Any]:
    steps = forks = rounds = reviews = blocking = fix_rounds = 0
    for line in events_path.read_text().splitlines():
        e = json.loads(line)
        if e["kind"] == "step":
            steps += 1
        elif e["kind"] == "fork_point":
            forks += 1
            rounds = max(rounds, int(e.get("round", 0)))
        elif e["kind"] == "review":
            reviews += 1
            blocking += int(e.get("blocking", 0))
        elif e["kind"] == "fix_round":
            fix_rounds += 1
    return {
        "steps": steps,
        "forks": forks,
        "rounds": rounds,
        "reviews": reviews,
        "blocking": blocking,
        "fix_rounds": fix_rounds,
    }
