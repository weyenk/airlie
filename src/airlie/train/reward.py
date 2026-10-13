"""Spec 03 §3 arithmetic, as plain functions so the trainer and the tests share them.

R(τ) = R_agg + λ_first·R_first                         (λ_first = 1.0)
A_base = (R − μ_R) / (σ_R + ε)                          (group-relative over the G trajectories of a task)
Â_t = A_base + β·1[t ∈ U ∧ R > 0]                        (usage-aware, β = 0.5)
L = Σ_t ( −log π(z_t|c_t)·Â_t + λ_sparse·π(YES|c_t) )    (λ_sparse = 0.05)
"""

from __future__ import annotations

import math

LAMBDA_FIRST = 1.0
BETA = 0.5
LAMBDA_SPARSE = 0.05
EPS = 1e-8


def episode_reward(*, r_agg: float, r_first: float, lambda_first: float = LAMBDA_FIRST) -> float:
    return r_agg + lambda_first * r_first


def group_relative(rewards: list[float], eps: float = EPS) -> list[float]:
    n = len(rewards)
    if n == 0:
        return []
    mu = sum(rewards) / n
    sd = math.sqrt(sum((r - mu) ** 2 for r in rewards) / n)
    return [(r - mu) / (sd + eps) for r in rewards]


def shaped_advantage(a_base: float, *, used: bool, reward: float, beta: float = BETA) -> float:
    return a_base + (beta if (used and reward > 0) else 0.0)


def loss_terms(
    *, logp_action: float, a_hat: float, p_yes: float, lambda_sparse: float = LAMBDA_SPARSE
) -> tuple[float, float]:
    """(policy term, sparsity term) for one step; the trainer sums both over the batch."""
    return -logp_action * a_hat, lambda_sparse * p_yes
