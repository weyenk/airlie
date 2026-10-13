"""ai-mode's decision policy (presets/agents/jev.md): clear / ambiguous / low-confidence."""

from __future__ import annotations

from typing import Literal

Decision = Literal["clear", "ambiguous", "low-confidence"]


def ranked(probabilities: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(((k, float(v)) for k, v in probabilities.items()), key=lambda kv: (-kv[1], kv[0]))


def policy(
    r: list[tuple[str, float]], confidence: float, *, ambiguous_gap: float, min_confidence: float
) -> Decision:
    if len(r) >= 2 and r[0][1] - r[1][1] < ambiguous_gap:
        return "ambiguous"
    if confidence < min_confidence:
        return "low-confidence"
    return "clear"
