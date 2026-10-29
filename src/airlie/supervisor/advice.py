"""How Muse's advice is added to a role's message. It imports nothing from airlie, so `layer_checks`, the
studio topology and the native team can all use it without a cycle.

The role's own text is kept whole and the advice goes after it: whatever Muse says, the rules in the message
(do not loosen a check, do not delete a failing test) are still there, unchanged, above it."""

from __future__ import annotations

SUPERVISOR = (
    "Supervisor: a second model read your work and the check's output. Its diagnosis and your next step:"
)


WRONG_TEST = "wrong_acceptance_test"
"""The triage category for a test the role may not change that asserts what correct code cannot give (roman,
2026-10-28: 'MMMDCCCLXXXIX' for 3999). Code cannot fix it, so it goes back to the architect at once instead of
to the coder for another round."""


def with_advice(text: str, advice: str | None) -> str:
    """`text` with the supervisor's section after it; `text` itself when there is no advice."""
    if not advice:
        return text
    return f"{text}\n\n{SUPERVISOR}\n{advice}"
