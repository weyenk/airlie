"""HeuristicController: named predicates that stand in for the learned controller until M5.

Rules, in order: empty observation → NO; tool error → NO; near-duplicate of an existing key (token Jaccard)
→ NO; tool class: results of tests, builds, searches, listings and edits are shareable, raw file reads are
not."""

from __future__ import annotations

import re

from airlie.lts.controller.protocol import AdmissionContext, Decision

SHAREABLE_TOOLS = {"run_tests", "shell", "grep", "list_dir", "write_file", "edit_file", "git"}
PRIVATE_TOOLS = {"read_file", "retrieve_value"}
_WORD = re.compile(r"[a-z0-9_]+")


def tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:  # pragma: no mutate  (`and` is equivalent: one empty side already gives 0 / n)
        return 0.0
    return len(ta & tb) / len(ta | tb)


class HeuristicController:
    kind = "heuristic"

    def __init__(self, duplicate_jaccard: float = 0.8) -> None:
        self.duplicate_jaccard = duplicate_jaccard

    async def decide(self, ctx: AdmissionContext) -> Decision:
        if not ctx.o_t.strip():
            return Decision(False, 0.0, "empty_observation")
        if not ctx.ok:
            return Decision(False, 0.05, "tool_error")
        for key in ctx.memory_keys:
            if jaccard(key, ctx.s_t) >= self.duplicate_jaccard:
                return Decision(False, 0.1, f"duplicate of existing key ({jaccard(key, ctx.s_t):.2f})")
        name = ctx.u_t.name
        if name in PRIVATE_TOOLS:
            return Decision(False, 0.2, "tool_class")
        if name in SHAREABLE_TOOLS or "." in name:
            return Decision(True, 0.8, "tool_class")
        return Decision(False, 0.3, "tool_class")
