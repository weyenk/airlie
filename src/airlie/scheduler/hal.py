"""HALSpeculativeScheduler, verbatim from docs/wiki/build-spec/04 §1 (Historical Accepted Length)."""

from __future__ import annotations

import numpy as np


class HALSpeculativeScheduler:
    def __init__(self, L_total: int = 16, L_min: int = 1, gamma: float = 0.3, tau: float = 1.5):  # noqa: N803
        self.L_total = L_total
        self.L_min = L_min
        self.gamma = gamma
        self.tau = tau
        self.hal_history: dict[str, float] = {}  # agent_id -> float

    def update_hal(self, agent_id: str, accepted_tokens: int) -> None:
        prev_hal = self.hal_history.get(agent_id, 4.0)
        new_hal = self.gamma * accepted_tokens + (1 - self.gamma) * prev_hal
        self.hal_history[agent_id] = new_hal

    def allocate_budgets(self, active_agent_ids: list[str]) -> dict[str, int]:
        N = len(active_agent_ids)  # noqa: N806
        if N == 0:
            return {}

        residual = self.L_total - N * self.L_min
        if residual <= 0:
            return {aid: self.L_min for aid in active_agent_ids}

        hals = np.array([self.hal_history.get(aid, 4.0) for aid in active_agent_ids])
        exp_hals = np.exp(hals / self.tau)
        probs = exp_hals / np.sum(exp_hals)

        allocations: dict[str, int] = {}
        for idx, aid in enumerate(active_agent_ids):
            extra = int(np.floor(residual * probs[idx]))
            allocations[aid] = self.L_min + extra
        return allocations
