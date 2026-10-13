"""Spec 03 §3 stepwise REINFORCE over logged admission decisions (the LTS paper's off-policy reuse of
trajectories): logged actions z_t, group-relative advantages per task, usage-aware shaping, sparsity
penalty."""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import torch

from airlie.lts.controller.learned import LTSAdmissionController
from airlie.train.dataset import Example
from airlie.train.reward import (
    BETA,
    LAMBDA_FIRST,
    LAMBDA_SPARSE,
    episode_reward,
    group_relative,
    shaped_advantage,
)


@dataclass
class TrainConfig:
    epochs: int = 5
    lr: float = 1e-4
    batch_size: int = 8
    lambda_sparse: float = LAMBDA_SPARSE
    beta: float = BETA
    lambda_first: float = LAMBDA_FIRST
    device: str | None = None
    seed: int = 0
    max_keys: int = 16


def advantages_by_group(
    examples: list[Example], *, lambda_first: float, beta: float
) -> dict[tuple[str, str, int], float]:
    """Â_t per (run, team, step): group-relative over one task's (run, team) trajectories, then shaped."""
    traj_reward: dict[tuple[str, str], float] = {}
    task_of: dict[tuple[str, str], str] = {}
    for e in examples:
        key = (e.run_id, e.team_id)
        traj_reward[key] = episode_reward(
            r_agg=e.team_reward, r_first=1.0 if e.first else 0.0, lambda_first=lambda_first
        )
        task_of[key] = e.task_key
    groups: dict[str, list[tuple[str, str]]] = {}
    for key, task in task_of.items():
        groups.setdefault(task, []).append(key)
    base: dict[tuple[str, str], float] = {}
    for keys in groups.values():
        rs = [traj_reward[k] for k in keys]
        for k, a in zip(keys, group_relative(rs), strict=True):
            base[k] = a
    out: dict[tuple[str, str, int], float] = {}
    for e in examples:
        k = (e.run_id, e.team_id)
        out[(e.run_id, e.team_id, e.step)] = shaped_advantage(
            base[k], used=e.used_later, reward=traj_reward[k], beta=beta
        )
    return out


def _triplet(e: Example, max_chars: int = 1500) -> tuple[str, str, str]:
    return (f"{e.u_t.get('name', '')}({str(e.u_t.get('arguments', ''))[:300]})", e.s_t, e.o_t[:max_chars])


def train(
    ctrl: LTSAdmissionController, examples: list[Example], cfg: TrainConfig, *, out_dir: Path
) -> dict[str, Any]:
    device = torch.device(cfg.device) if cfg.device else next(ctrl.parameters()).device
    ctrl.to(device)
    ctrl.train()
    params = [p for p in ctrl.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=cfg.lr)
    adv = advantages_by_group(examples, lambda_first=cfg.lambda_first, beta=cfg.beta)
    rng = random.Random(cfg.seed)
    metrics: dict[str, Any] = {
        "epoch_loss": [],
        "epoch_yes_rate": [],
        "n_examples": len(examples),
        "config": asdict(cfg),
    }
    t0 = time.monotonic()
    for _epoch in range(cfg.epochs):
        order = list(examples)
        rng.shuffle(order)
        total = 0.0
        yes_sum = 0.0
        for i in range(0, len(order), cfg.batch_size):
            batch = order[i : i + cfg.batch_size]
            opt.zero_grad()
            loss = torch.zeros((), device=device)
            for e in batch:
                c_t = ctrl.encode(e.query, e.memory_keys[-cfg.max_keys :], _triplet(e)).to(device)
                logits = ctrl(c_t)  # [1, 2] = [NO, YES]
                logp = torch.log_softmax(logits, dim=-1)[0]
                z = 1 if e.action == "YES" else 0
                a_hat = adv[(e.run_id, e.team_id, e.step)]
                p_yes = torch.softmax(logits, dim=-1)[0, 1]
                loss = loss + (-logp[z] * a_hat) + cfg.lambda_sparse * p_yes
                yes_sum += float(p_yes.detach())
            loss = loss / max(len(batch), 1)
            loss.backward()  # type: ignore[no-untyped-call]
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            opt.step()
            total += float(loss.detach()) * len(batch)
        metrics["epoch_loss"].append(total / max(len(examples), 1))
        metrics["epoch_yes_rate"].append(yes_sum / max(len(examples), 1))
    metrics["train_seconds"] = round(time.monotonic() - t0, 1)
    ctrl.eval()
    ctrl.save(out_dir)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=1))
    return metrics


def _scores(decisions: list[bool], examples: list[Example]) -> dict[str, float]:
    useful = [e.used_later for e in examples]
    n = len(examples)
    yes = sum(decisions)
    tp = sum(1 for d, u in zip(decisions, useful, strict=True) if d and u)
    return {
        "n": float(n),
        "yes_rate": round(yes / n, 3) if n else 0.0,
        "useful_precision": round(tp / yes, 3) if yes else 0.0,
        "useful_recall": round(tp / sum(useful), 3) if sum(useful) else 0.0,
    }


def evaluate(
    ctrl: LTSAdmissionController, examples: list[Example], *, threshold: float = 0.5, max_keys: int = 16
) -> dict[str, Any]:
    """Replay held-out decisions: the learned controller vs the logged (heuristic/judge) decisions, scored by
    whether the admitted entry was later retrieved by a peer (`used_later`), the usage signal the paper's
    shaping rewards."""
    ctrl.eval()
    learned: list[bool] = []
    agree = 0
    with torch.no_grad():
        for e in examples:
            p = ctrl.p_yes(ctrl.encode(e.query, e.memory_keys[-max_keys:], _triplet(e)))
            d = p >= threshold
            learned.append(d)
            agree += int(d == (e.action == "YES"))
    logged = [e.action == "YES" for e in examples]
    return {
        "n": len(examples),
        "learned": _scores(learned, examples),
        "logged": _scores(logged, examples),
        "agreement": round(agree / len(examples), 3) if examples else 0.0,
        "useful_fraction": round(sum(e.used_later for e in examples) / len(examples), 3) if examples else 0.0,
    }
