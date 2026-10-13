"""Spec 03 §1: the Global Shared Memory Bank M = {(s_i, o_i)} and its entries, names verbatim.

Additions beyond the spec: optional JSONL persistence (one line per add/retrieve) so a run's bank can be
inspected with `task memory` and replayed by M5's dataset builder; `entries()` and `new_id()` helpers."""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class MemoryEntry(BaseModel):
    id: str = Field(description="Unique UUID string for the memory entry")
    team_id: str = Field(description="ID of the parallel team that produced the step")
    step_num: int = Field(description="Sequential step number within team trajectory")
    summary_key: str = Field(description="Short, 1-sentence natural language summary (Key)")
    raw_value: str = Field(description="Full output content or code result (Value)")
    timestamp: float = Field(default_factory=lambda: datetime.datetime.now().timestamp())
    access_count: int = Field(default=0, description="Number of times accessed by other teams")


class SharedMemoryBank:
    def __init__(self, persist_path: Path | None = None) -> None:
        self._entries: dict[str, MemoryEntry] = {}
        self._persist = persist_path
        if persist_path is not None:
            persist_path.parent.mkdir(parents=True, exist_ok=True)

    def add_entry(self, entry: MemoryEntry) -> None:
        self._entries[entry.id] = entry
        self._log({"kind": "add", "entry": entry.model_dump()})

    def get_summaries(self) -> list[tuple[str, str]]:
        """Returns list of (entry_id, summary_key) for prompt injection."""
        return [(e.id, e.summary_key) for e in self._entries.values()]

    def retrieve_value(self, entry_id: str, requesting_team_id: str) -> str | None:
        """Retrieves raw output value and updates cross-team access stats."""
        entry = self._entries.get(entry_id)
        if entry:
            if entry.team_id != requesting_team_id:
                entry.access_count += 1
            self._log({"kind": "retrieve", "entry_id": entry_id, "requesting_team_id": requesting_team_id})
            return entry.raw_value
        return None

    # ---- helpers beyond the spec ----
    def entries(self) -> list[MemoryEntry]:
        return list(self._entries.values())

    def new_id(self, team_id: str, step_num: int) -> str:
        return f"mem-{team_id}-{step_num}-{len(self._entries) + 1}"

    def __len__(self) -> int:
        return len(self._entries)

    def _log(self, rec: dict[str, Any]) -> None:
        if self._persist is None:
            return
        with self._persist.open("a") as f:
            f.write(json.dumps({**rec, "ts": time.time()}, default=str) + "\n")

    @classmethod
    def load(cls, path: Path) -> SharedMemoryBank:
        """Rebuild a bank from its JSONL log (without re-persisting)."""
        bank = cls()
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec["kind"] == "add":
                bank._entries[rec["entry"]["id"]] = MemoryEntry(**rec["entry"])
            elif rec["kind"] == "retrieve":
                e = bank._entries.get(rec["entry_id"])
                if e and e.team_id != rec["requesting_team_id"]:
                    e.access_count += 1
        return bank
