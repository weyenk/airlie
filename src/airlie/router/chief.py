"""ChiefRouter: the plan's own `role` field is the decision (design §6)."""

from __future__ import annotations

from airlie.router.protocol import ROLE_QUESTION, RouteDecision


class ChiefRouter:
    async def route(
        self,
        brief: str,
        criteria: dict[str, str],
        hint: str | None = None,
        *,
        question: str = "role",
        instructions: str = ROLE_QUESTION,
    ) -> RouteDecision:
        if hint is None or hint not in criteria:
            raise ValueError(
                f"chief router needs a role hint from the closed set; got {hint!r} (set: {sorted(criteria)})"
            )
        return RouteDecision(question, hint, hint, "hint", {hint: 1.0}, 1.0, hint)
