"""The supervisor's ledger: every Muse intervention, what came of it, and Noah's verdict on it, in SQLite
(stdlib sqlite3, WAL) beside the board. It is append-only: an outcome or a verdict is a new row, the latest
one wins, and SQL triggers refuse any UPDATE or DELETE, so the record of what Muse said cannot be rewritten
after the fact, by a bug or by a model.

Fallback rows (Muse errored, timed out or was capped out) are recorded too, with no answer and `fallback` set,
so the rate at which the supervisor stood aside is as visible as the rate at which it helped."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from airlie.supervisor.answer import Answer

Trigger = Literal[
    "precode_retry",
    "static_retry",
    "gate_retry",
    "amend",
    "repeat_guard",
    "block",
    "package_retry",
    "replan",
]
Outcome = Literal["helped", "not_helped", "blocked", "unknown"]
Verdict = Literal["right", "wrong"]

_TABLES = ("interventions", "outcomes", "verdicts")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY, at REAL NOT NULL, story TEXT NOT NULL, layer TEXT NOT NULL,
    studio_run TEXT NOT NULL, subtask TEXT NOT NULL, role TEXT NOT NULL, trigger TEXT NOT NULL,
    kinds_json TEXT NOT NULL, category TEXT, owner TEXT, diagnosis TEXT, evidence_json TEXT,
    message TEXT, would_route_to TEXT, fix_if_attempt TEXT, fallback TEXT,
    model TEXT, seconds REAL, tokens INTEGER);
CREATE INDEX IF NOT EXISTS interventions_by_layer ON interventions(story, layer, id);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY, intervention_id INTEGER NOT NULL, at REAL NOT NULL,
    outcome TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS outcomes_by_intervention ON outcomes(intervention_id, id);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY, intervention_id INTEGER NOT NULL, at REAL NOT NULL,
    verdict TEXT NOT NULL, note TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS verdicts_by_intervention ON verdicts(intervention_id, id);
""" + "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""
    for t in _TABLES
)

_SELECT = """
SELECT i.*,
  (SELECT outcome FROM outcomes o WHERE o.intervention_id = i.id ORDER BY o.id DESC LIMIT 1) AS outcome,
  (SELECT verdict FROM verdicts v WHERE v.intervention_id = i.id ORDER BY v.id DESC LIMIT 1) AS verdict
FROM interventions i
"""


class LedgerError(ValueError):
    """An outcome or verdict for an intervention that is not in the ledger; nothing was stored."""


@dataclass(frozen=True)
class Intervention:
    """One thing Muse was asked and what it said (`answer`), or why it said nothing (`fallback`)."""

    story: str
    layer: str
    studio_run: str
    subtask: str
    role: str
    trigger: Trigger
    kinds: tuple[str, ...]
    answer: Answer | None
    fallback: str | None
    model: str
    seconds: float
    tokens: int


@dataclass(frozen=True)
class Row:
    id: int
    at: float
    story: str
    layer: str
    studio_run: str
    subtask: str
    role: str
    trigger: Trigger
    kinds: tuple[str, ...]
    category: str | None
    owner: str | None
    diagnosis: str | None
    evidence: tuple[str, ...]
    message: str | None
    would_route_to: str | None
    fix_if_attempt: str | None
    fallback: str | None
    model: str | None
    seconds: float | None
    tokens: int | None
    outcome: Outcome | None  # the latest recorded
    verdict: Verdict | None  # the latest recorded


@dataclass(frozen=True)
class Stats:
    """The trial's numbers. Help is counted per retry, as (retries, helped), on two sides: `by_trigger` and
    `by_category` the retries that carried Muse's answer, `fallback_by_trigger` the ones where it stood aside
    and the role got today's text alone, the no-Muse baseline to set beside them. A block ends the layer, so
    its outcome is always `blocked`: blocks are counted on their own (`blocks`), in neither side."""

    by_trigger: dict[str, tuple[int, int]]  # answered retries: (interventions, helped)
    fallback_by_trigger: dict[str, tuple[int, int]]  # fallback retries: (interventions, helped)
    by_category: dict[str, tuple[int, int]]  # answered retries, by Muse's category
    blocks: int
    fallbacks: int  # every row where Muse stood aside, blocks included
    total: int
    blocks_with_fix: list[int]  # intervention ids: Muse blocked a layer but named a fix worth attempting


def _row(r: sqlite3.Row) -> Row:
    return Row(
        id=r["id"],
        at=r["at"],
        story=r["story"],
        layer=r["layer"],
        studio_run=r["studio_run"],
        subtask=r["subtask"],
        role=r["role"],
        trigger=cast(Trigger, r["trigger"]),
        kinds=tuple(json.loads(r["kinds_json"])),
        category=r["category"],
        owner=r["owner"],
        diagnosis=r["diagnosis"],
        evidence=tuple(json.loads(r["evidence_json"] or "[]")),
        message=r["message"],
        would_route_to=r["would_route_to"],
        fix_if_attempt=r["fix_if_attempt"],
        fallback=r["fallback"],
        model=r["model"],
        seconds=r["seconds"],
        tokens=r["tokens"],
        outcome=cast("Outcome | None", r["outcome"]),
        verdict=cast("Verdict | None", r["verdict"]),
    )


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ---- writing -------------------------------------------------------------------------------------------

    def record(self, i: Intervention) -> int:
        """Append `i`; the id it is known by from here on."""
        a = i.answer
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO interventions (at, story, layer, studio_run, subtask, role, trigger, kinds_json,"
                " category, owner, diagnosis, evidence_json, message, would_route_to, fix_if_attempt,"
                " fallback, model, seconds, tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    i.story,
                    i.layer,
                    i.studio_run,
                    i.subtask,
                    i.role,
                    i.trigger,
                    json.dumps(list(i.kinds)),
                    a.category if a else None,
                    a.owner if a else None,
                    a.diagnosis if a else None,
                    json.dumps(list(a.evidence)) if a else None,
                    a.message if a else None,
                    a.would_route_to if a else None,
                    a.fix_if_attempt if a else None,
                    i.fallback,
                    i.model,
                    i.seconds,
                    i.tokens,
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def outcome(self, intervention_id: int, outcome: Outcome, detail: str = "") -> None:
        """What came of the intervention; a later call supersedes an earlier one but never erases it."""
        self._append("outcomes", "outcome", intervention_id, outcome, detail)

    def verdict(self, intervention_id: int, verdict: Verdict, note: str = "") -> None:
        """Noah's judgement of the intervention; the latest wins."""
        self._append("verdicts", "verdict", intervention_id, verdict, note)

    def _append(self, table: str, column: str, intervention_id: int, value: str, text: str) -> None:
        extra = "detail" if table == "outcomes" else "note"
        with self._lock:
            if (
                self._db.execute("SELECT 1 FROM interventions WHERE id = ?", (intervention_id,)).fetchone()
                is None
            ):
                raise LedgerError(f"no intervention {intervention_id} in the ledger")
            self._db.execute(
                f"INSERT INTO {table} (intervention_id, at, {column}, {extra}) VALUES (?,?,?,?)",
                (intervention_id, time.time(), value, text),
            )

    # ---- reading -------------------------------------------------------------------------------------------

    def _rows(
        self, where: str = "", args: Sequence[object] = (), tail: str = "ORDER BY i.id DESC"
    ) -> list[Row]:
        with self._lock:
            return [_row(r) for r in self._db.execute(f"{_SELECT} {where} {tail}", tuple(args))]

    def recent(self, limit: int = 20) -> list[Row]:
        """The newest `limit` interventions, newest first."""
        return self._rows(tail="ORDER BY i.id DESC LIMIT ?", args=(limit,))

    def for_layer(self, story: str, layer: str) -> list[Row]:
        """Every intervention in one layer of one story, newest first."""
        return self._rows("WHERE i.story = ? AND i.layer = ?", (story, layer))

    def recall(self, trigger: Trigger, kinds: Sequence[str], limit: int = 3) -> list[Row]:
        """Past answers that helped on the same trigger and share a kind of finding with `kinds`, newest
        first: what worked before in a case like this one. A fallback row is never one: it helped, if it did,
        with today's text, and has no fix to show."""
        wanted = set(kinds)
        rows = self._rows("WHERE i.trigger = ? AND i.fallback IS NULL AND i.message != ''", (trigger,))
        return [r for r in rows if r.outcome == "helped" and wanted & set(r.kinds)][:limit]

    def stats(self) -> Stats:
        rows = self._rows()
        by_trigger: dict[str, tuple[int, int]] = {}
        fallback_by_trigger: dict[str, tuple[int, int]] = {}
        by_category: dict[str, tuple[int, int]] = {}

        def count(table: dict[str, tuple[int, int]], key: str, helped: int) -> None:
            n, h = table.get(key, (0, 0))
            table[key] = (n + 1, h + helped)

        for r in rows:
            if r.trigger == "block":
                continue
            helped = 1 if r.outcome == "helped" else 0
            if r.fallback is not None:
                count(fallback_by_trigger, r.trigger, helped)
                continue
            count(by_trigger, r.trigger, helped)
            if r.category is not None:
                count(by_category, r.category, helped)
        return Stats(
            by_trigger=by_trigger,
            fallback_by_trigger=fallback_by_trigger,
            by_category=by_category,
            blocks=sum(1 for r in rows if r.trigger == "block"),
            fallbacks=sum(1 for r in rows if r.fallback is not None),
            total=len(rows),
            blocks_with_fix=sorted(r.id for r in rows if r.trigger == "block" and r.fix_if_attempt),
        )
