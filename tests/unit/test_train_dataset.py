"""Dataset from admission logs and the spec 03 §3 reward/advantage/loss arithmetic."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from airlie.train.dataset import Example, build_dataset, split_by_run
from airlie.train.reward import episode_reward, group_relative, loss_terms, shaped_advantage


def write_run(
    state: Path, run_id: str, goal: str, rows: list[dict], team_pass: dict[str, bool], first: str
) -> None:
    rd = state / "runs" / run_id
    (rd / "lts").mkdir(parents=True)
    with (rd / "lts" / "admission.jsonl").open("w") as f:
        for r in rows:
            base = {
                "run_id": run_id,
                "query": goal,
                "memory_keys": [],
                "u_t": {"name": "shell", "arguments": "{}"},
                "s_t": "s",
                "o_t": "o",
                "p_yes": 0.5,
                "reason": "r",
                "controller": "heuristic",
                "ts": 1.0,
                "used_later": False,
                "episode_reward": None,
            }
            f.write(json.dumps({**base, **r}) + "\n")
    events = [{"kind": "run_start", "goal": goal, "k": len(team_pass)}]
    order = [first] + [t for t in team_pass if t != first]
    for i, t in enumerate(order):
        events.append(
            {
                "kind": "team_done",
                "team_id": t,
                "ok": team_pass[t],
                "wall_ms": 1000.0 * (i + 1),
                "ts": float(i),
            }
        )
    events.append(
        {
            "kind": "aggregate",
            "winner": first,
            "scores": {t: (1.0 if p else 0.0) for t, p in team_pass.items()},
        }
    )
    (rd / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def test_build_dataset_reads_rows_rewards_and_first(tmp_path: Path) -> None:
    write_run(
        tmp_path,
        "r1",
        "goal A",
        [
            {
                "team_id": "team-1",
                "step": 1,
                "decision": True,
                "entry_id": "m1",
                "used_later": True,
                "episode_reward": 1.0,
            },
            {"team_id": "team-2", "step": 1, "decision": False, "entry_id": None, "episode_reward": 0.0},
        ],
        {"team-1": True, "team-2": False},
        first="team-1",
    )
    ds = build_dataset(tmp_path)
    assert len(ds) == 2
    ex = ds[0]
    assert isinstance(ex, Example) and ex.action == "YES" and ex.used_later is True
    assert ex.team_reward == 1.0 and ex.first is True and ex.aggregate_reward == 1.0
    assert ds[1].action == "NO" and ds[1].first is False and ds[1].team_reward == 0.0
    assert ds[0].task_key == ds[1].task_key


def test_split_by_run_keeps_runs_whole(tmp_path: Path) -> None:
    for i in range(4):
        write_run(
            tmp_path,
            f"r{i}",
            "goal",
            [{"team_id": "team-1", "step": 1, "decision": True, "entry_id": "m"}],
            {"team-1": True},
            first="team-1",
        )
    ds = build_dataset(tmp_path)
    train, hold = split_by_run(ds, holdout_fraction=0.25, seed=1)
    assert len(train) == 3 and len(hold) == 1
    assert not ({e.run_id for e in train} & {e.run_id for e in hold})


def test_reward_formulas_match_the_spec() -> None:
    assert episode_reward(r_agg=1.0, r_first=1.0, lambda_first=1.0) == 2.0
    rs = [2.0, 0.0, 1.0]
    adv = group_relative(rs)
    mu = sum(rs) / 3
    sd = math.sqrt(sum((r - mu) ** 2 for r in rs) / 3)
    assert adv[0] == pytest.approx((2.0 - mu) / (sd + 1e-8))
    assert shaped_advantage(0.3, used=True, reward=1.0, beta=0.5) == pytest.approx(0.8)
    assert shaped_advantage(0.3, used=True, reward=0.0, beta=0.5) == pytest.approx(0.3)
    assert shaped_advantage(0.3, used=False, reward=1.0, beta=0.5) == pytest.approx(0.3)


def test_loss_terms() -> None:
    policy, sparsity = loss_terms(logp_action=math.log(0.5), a_hat=2.0, p_yes=0.5, lambda_sparse=0.05)
    assert policy == pytest.approx(-math.log(0.5) * 2.0)
    assert sparsity == pytest.approx(0.025)
