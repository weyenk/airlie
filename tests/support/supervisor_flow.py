"""The supervisor's scripted triage for story runs: a `supervisor` role on FakeEngine, Muse's answers, and
the ledger a run wrote. Shared by test_story_supervisor_flow and the layer-build golden."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.engine import FakeEngine, FakeReply
from airlie.supervisor.ledger import Ledger, Row
from tests.support.story import STORY
from tests.support.story_layer_checks_flow import LAYERS, RED
from tests.support.studio import APPROVE, ROLES

SUPERVISOR_ROLES = {
    **ROLES,
    "product": ("---\nsummary: Stories and acceptance criteria\n---\n# product", ["read_file", "list_dir"]),
    "architect": ("---\nsummary: Layered plans for a change\n---\n# architect", ["read_file", "list_dir"]),
    "supervisor": ("---\nsummary: no\nroutable: false\n---\n# supervisor\nJSON only.", []),
}


def answer(
    message: str | None,
    *,
    category: str = "missing_code",
    owner: str = "architect",
    fix: str | None = None,
    class_agrees: bool | None = None,
    class_reason: str | None = None,
) -> FakeReply:
    """Muse's answer: a diagnosis, the role's next step and, for a block, the fix another try would make."""
    data = {
        "category": category,
        "owner": owner,
        "diagnosis": "the code breaks add, which the whole suite checks",
        "evidence": ["calc.py"],
        "message": message,
        "fix_if_attempt": fix,
        "class_agrees": class_agrees,
        "class_reason": class_reason,
    }
    return FakeReply(text="```json\n" + json.dumps(data) + "\n```")


def scripted(
    *,
    architect_build: list[FakeReply] | None = None,
    coder: list[FakeReply] | None = None,
    supervisor: list[FakeReply] | None = None,
    packages: list[FakeReply] | None = None,
) -> FakeEngine:
    """The one-layer calc story's replies, by role; no `supervisor` key when `supervisor` is None."""
    build = architect_build if architect_build is not None else (coder or [])
    scripts = {
        "product": [STORY],
        "architect": [LAYERS, *(packages or [RED]), *build],
        "reviewer": [APPROVE, APPROVE],
    }
    if supervisor is not None:
        scripts["supervisor"] = supervisor
    return FakeEngine(scripts)


def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "state" / "supervisor.sqlite")


def rows(tmp_path: Path) -> list[Row]:
    return ledger(tmp_path).recent()
