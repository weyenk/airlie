"""Shared helpers extracted from tests/unit/test_supervisor_triage.py for tests.support.supervisor_triage."""

from __future__ import annotations

from typing import Any

GOOD: dict[str, Any] = {
    "category": "missing_import",
    "owner": "architect",
    "diagnosis": "Path is not imported.",
    "evidence": ["NameError: name 'Path' is not defined"],
    "message": "Import Path, then edit.",
    "would_route_to": "architect",
    "fix_if_attempt": None,
}
