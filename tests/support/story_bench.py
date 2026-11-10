"""Shared helpers extracted from tests/unit/test_story_bench.py for tests.support.story_bench."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CASES = Path(__file__).resolve().parents[2] / "evals" / "stories"


def write_events(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
