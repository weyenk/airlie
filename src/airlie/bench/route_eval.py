"""route:eval — accuracy, mean confidence, wrong-but-clear and confusions over a labelled routing set."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from airlie.router.protocol import Router


@dataclass(frozen=True)
class Case:
    task: str
    expect: str
    question: str = "role"


@dataclass
class RoutingReport:
    n: int
    correct: int
    accuracy: float
    mean_confidence: float
    wrong_but_clear: int
    confusions: dict[str, int]
    rows: list[dict[str, Any]] = field(default_factory=list)


def load_cases(path: Path) -> list[Case]:
    out: list[Case] = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        r = json.loads(line)
        out.append(Case(str(r["task"]), str(r["expect"]), str(r.get("question", "role"))))
    return out


def load_criteria(path: Path) -> dict[str, str]:
    data = yaml.safe_load(path.read_text()) or {}
    return {str(k): str(v) for k, v in data.items()}


async def evaluate(router: Router, cases: list[Case], criteria: dict[str, str]) -> RoutingReport:
    rows: list[dict[str, Any]] = []
    correct = 0
    conf_sum = 0.0
    wrong_clear = 0
    confusions: Counter[str] = Counter()
    for c in cases:
        d = await router.route(c.task, criteria, None, question=c.question)
        ok = d.chosen == c.expect
        correct += int(ok)
        conf_sum += d.confidence
        if not ok:
            confusions[f"{c.expect}->{d.chosen}"] += 1
            if d.decision == "clear":
                wrong_clear += 1
        rows.append(
            {
                "task": c.task,
                "expect": c.expect,
                "chosen": d.chosen,
                "decision": d.decision,
                "confidence": d.confidence,
                "ok": ok,
            }
        )
    n = len(cases)
    return RoutingReport(
        n, correct, correct / n if n else 0.0, conf_sum / n if n else 0.0, wrong_clear, dict(confusions), rows
    )


def render(rep: RoutingReport) -> str:
    lines = [
        f"routing eval: {rep.correct}/{rep.n} accuracy {rep.accuracy:.2f} "
        f"mean-confidence {rep.mean_confidence:.2f} wrong-but-clear {rep.wrong_but_clear}",
    ]
    for pair, k in sorted(rep.confusions.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  confusion {pair} x{k}")
    for r in rep.rows:
        if not r["ok"]:
            head = f"  miss [{r['decision']} {r['confidence']:.2f}] {r['expect']} -> {r['chosen']}"
            lines.append(f"{head}: {r['task'][:80]}")
    return "\n".join(lines)


def save(rep: RoutingReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(rep), indent=1))
    return path
