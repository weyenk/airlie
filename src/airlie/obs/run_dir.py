"""Per-run directory: runs/<run_id>/{events.jsonl, trace/, team-<i>/, report.md}."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


@dataclass(frozen=True)
class RunDir:
    path: Path
    run_id: str

    @classmethod
    def create(cls, state_dir: Path, run_id: str | None = None) -> RunDir:
        rid = run_id or new_run_id()
        p = state_dir.expanduser() / "runs" / rid
        (p / "trace").mkdir(parents=True, exist_ok=True)
        return cls(p, rid)

    @property
    def events_path(self) -> Path:
        return self.path / "events.jsonl"

    def write_trace(self, rel: str, payload: dict[str, Any]) -> Path:
        out = self.path / "trace" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1, default=str))
        return out
