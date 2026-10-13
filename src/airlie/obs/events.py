"""JSON-lines event sink: one line per step, tool, admission, retrieval, engine stat."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class EventSink:
    def __init__(self, path: Path, run_id: str | None = None) -> None:
        self.path = path
        self.run_id = run_id or path.parent.name
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, **fields: Any) -> None:
        rec = {"ts": time.time(), "run_id": self.run_id, "kind": kind, **fields}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
