"""Spec 04 §3 prompt injection: companion teams see the summary keys at the start of each step."""

from __future__ import annotations

HEADER = "\n[Shared Memory Keys Available]:\n"


def render_keys(summaries: list[tuple[str, str]], *, max_keys: int) -> str:
    """Newest keys first (the bank keeps insertion order), capped at max_keys; empty when nothing to show."""
    if not summaries or max_keys <= 0:
        return ""
    chosen = list(reversed(summaries[-max_keys:]))
    return HEADER + "\n".join(f"- Key [{sid}]: {skey}" for sid, skey in chosen)
