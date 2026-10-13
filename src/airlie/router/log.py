"""RoutingLog: one JSON line per decision (runs/<id>/routing.jsonl), scored by `task route:eval`."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from airlie.router.protocol import RouteDecision


class RoutingLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, subtask: str, d: RouteDecision) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "subtask": subtask, **asdict(d)}) + "\n")
