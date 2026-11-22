"""Sqlite store for which live model ids airlie may use."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS allowlist (
    id TEXT PRIMARY KEY,
    allowed INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ModelRow:
    id: str
    allowed: bool
    label: str | None


class ModelAllowlist:
    def __init__(self, path: Path) -> None:
        path = path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def is_allowed(self, model_id: str) -> bool:
        row = self._db.execute("SELECT allowed FROM allowlist WHERE id = ?", (model_id,)).fetchone()
        if row is None:
            return True
        return bool(row[0])

    def set_allowed(self, model_id: str, allowed: bool) -> None:
        self._db.execute(
            "INSERT INTO allowlist (id, allowed) VALUES (?, ?)"
            " ON CONFLICT(id) DO UPDATE SET allowed = excluded.allowed",
            (model_id, int(allowed)),
        )

    def stored_ids(self) -> list[str]:
        """Every id with a saved choice, sorted."""
        return [r[0] for r in self._db.execute("SELECT id FROM allowlist ORDER BY id")]

    def close(self) -> None:
        self._db.close()

    def merge(
        self,
        live_ids: list[str],
        labels: Mapping[str, str] | None = None,
    ) -> list[ModelRow]:
        label_map = labels or {}
        rows: list[ModelRow] = []
        for model_id in live_ids:
            rows.append(
                ModelRow(
                    id=model_id,
                    allowed=self.is_allowed(model_id),
                    label=label_map.get(model_id),
                )
            )
        return rows
