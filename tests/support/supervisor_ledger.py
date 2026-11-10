"""Shared helpers extracted from tests/unit/test_supervisor_ledger.py for tests.support.supervisor_ledger."""

from __future__ import annotations

from typing import Any

from airlie.supervisor.answer import Answer
from airlie.supervisor.ledger import Intervention


def intervention(**kw: Any) -> Intervention:
    answer = kw.pop("answer", "default")
    if answer == "default":
        answer = Answer(
            category=kw.pop("category", "missing_import"),
            owner="coder",
            diagnosis="the import is missing",
            evidence=("line 3",),
            message="add the import",
            would_route_to="coder",
            fix_if_attempt=kw.pop("fix_if_attempt", None),
        )
    return Intervention(
        story=kw.pop("story", "AIR-1"),
        layer=kw.pop("layer", "01"),
        studio_run="run-1",
        subtask="s1",
        role="coder",
        trigger=kw.pop("trigger", "precode_retry"),
        kinds=kw.pop("kinds", ("passes_before_code",)),
        answer=answer,
        fallback=kw.pop("fallback", None),
        model="muse",
        seconds=1.5,
        tokens=100,
    )
