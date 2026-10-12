"""Examples for the controller from every run's `lts/admission.jsonl` (+ `events.jsonl` for outcomes).

One Example per logged admission decision: the context the controller saw (query, memory keys, step
triplet), the action it took (YES/NO), whether a peer later retrieved the entry, the team's test reward,
whether the team was the first finisher (R_first), and the run's aggregate reward (R_agg)."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Example:
    run_id: str
    task_key: str
    team_id: str
    step: int
    query: str
    memory_keys: list[str]
    u_t: dict[str, str]
    s_t: str
    o_t: str
    action: str  # "YES" | "NO"
    used_later: bool
    team_reward: float
    first: bool
    aggregate_reward: float
    p_yes_logged: float = 0.5
    controller: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _run_outcome(events_path: Path) -> tuple[str, str | None, dict[str, float], dict[str, bool]]:
    goal = ""
    first: str | None = None
    first_t = float("inf")
    scores: dict[str, float] = {}
    ok: dict[str, bool] = {}
    if not events_path.exists():
        return goal, first, scores, ok
    for line in events_path.read_text().splitlines():
        e = json.loads(line)
        if e["kind"] == "run_start":
            goal = str(e.get("goal", ""))
        elif e["kind"] == "team_done":
            ok[e["team_id"]] = bool(e.get("ok"))
            t = float(e.get("ts", 0.0))
            if e.get("ok") and t < first_t:
                first, first_t = e["team_id"], t
        elif e["kind"] == "aggregate":
            scores = {k: float(v) for k, v in (e.get("scores") or {}).items()}
    return goal, first, scores, ok


def build_dataset(state_dir: Path) -> list[Example]:
    out: list[Example] = []
    runs = sorted((state_dir / "runs").glob("*/lts/admission.jsonl"))
    for adm in runs:
        run_dir = adm.parents[1]
        goal, first, scores, _ = _run_outcome(run_dir / "events.jsonl")
        agg_reward = max(scores.values(), default=0.0)
        for line in adm.read_text().splitlines():
            r = json.loads(line)
            query = str(r.get("query") or goal)
            team_reward = r.get("episode_reward")
            if team_reward is None:
                team_reward = scores.get(r["team_id"], 0.0)
            out.append(
                Example(
                    run_id=run_dir.name,
                    task_key=hashlib.sha1(query.strip().encode()).hexdigest()[:12],
                    team_id=str(r["team_id"]),
                    step=int(r.get("step", 0)),
                    query=query,
                    memory_keys=list(r.get("memory_keys") or []),
                    u_t=dict(r.get("u_t") or {}),
                    s_t=str(r.get("s_t", "")),
                    o_t=str(r.get("o_t", "")),
                    action="YES" if r.get("decision") else "NO",
                    used_later=bool(r.get("used_later")),
                    team_reward=float(team_reward),
                    first=(first is not None and r["team_id"] == first),
                    aggregate_reward=float(agg_reward),
                    p_yes_logged=float(r.get("p_yes", 0.5)),
                    controller=str(r.get("controller", "")),
                )
            )
    return out


def split_by_run(
    examples: list[Example], *, holdout_fraction: float, seed: int = 0
) -> tuple[list[Example], list[Example]]:
    runs = sorted({e.run_id for e in examples})
    rng = random.Random(seed)
    rng.shuffle(runs)
    n_hold = max(1, round(len(runs) * holdout_fraction)) if len(runs) > 1 else 0
    hold = set(runs[:n_hold])
    return [e for e in examples if e.run_id not in hold], [e for e in examples if e.run_id in hold]


def save_dataset(examples: list[Example], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in examples:
            f.write(json.dumps(e.__dict__, default=str) + "\n")
    return path


def load_dataset(path: Path) -> list[Example]:
    return [Example(**json.loads(line)) for line in path.read_text().splitlines()]
