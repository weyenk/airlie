import sqlite3
from pathlib import Path
from typing import Any

import pytest

from airlie.supervisor.answer import Answer
from airlie.supervisor.ledger import Intervention, Ledger, LedgerError


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


def test_a_recorded_intervention_reads_back_with_its_latest_outcome(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "supervisor.sqlite")
    i = led.record(intervention(trigger="precode_retry", category="missing_import"))
    led.outcome(i, "not_helped")
    led.outcome(i, "helped", "check passed")
    [row] = led.recent()
    assert (row.id, row.category, row.outcome) == (i, "missing_import", "helped")
    assert row.evidence == ("line 3",) and row.kinds == ("passes_before_code",) and row.verdict is None


def test_the_ledger_refuses_updates_and_deletes(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.sqlite")
    i = led.record(intervention())
    led.outcome(i, "helped")
    led.verdict(i, "right")
    con = sqlite3.connect(tmp_path / "s.sqlite")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE interventions SET category = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM outcomes")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE verdicts SET note = 'x'")
    assert led.recent()[0].verdict == "right"


def test_a_verdict_on_an_unknown_id_is_refused(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.sqlite")
    with pytest.raises(LedgerError):
        led.verdict(99, "right")
    with pytest.raises(LedgerError):
        led.outcome(99, "helped")


def test_a_fallback_row_has_no_answer_and_counts_as_a_fallback(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.sqlite")
    led.record(intervention(answer=None, fallback="timeout"))
    assert led.stats().fallbacks == 1 and led.recent()[0].category is None
    assert led.recent()[0].fallback == "timeout" and led.recent()[0].evidence == ()


def test_recall_returns_only_helped_rows_with_the_same_trigger_and_a_shared_kind(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.sqlite")
    a = led.record(intervention(trigger="precode_retry", kinds=("passes_before_code",)))
    led.outcome(a, "helped")
    b = led.record(intervention(trigger="precode_retry", kinds=("passes_before_code",)))
    led.outcome(b, "not_helped")
    c = led.record(intervention(trigger="gate_retry", kinds=("passes_before_code",)))
    led.outcome(c, "helped")
    d = led.record(intervention(trigger="precode_retry", kinds=("other_kind",)))
    led.outcome(d, "helped")
    assert [r.id for r in led.recall("precode_retry", ["passes_before_code"])] == [a]


def test_stats_count_help_by_trigger_and_category_and_list_blocks_with_a_fix(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.sqlite")
    a = led.record(intervention(trigger="precode_retry", category="missing_import"))
    led.outcome(a, "helped")
    b = led.record(intervention(trigger="block", fix_if_attempt="add the import"))
    led.outcome(b, "blocked")
    s = led.stats()
    assert s.by_trigger["precode_retry"] == (1, 1) and s.by_category["missing_import"] == (2, 1)
    assert s.blocks_with_fix == [b] and s.total == 2


def test_a_fallback_counts_by_trigger_but_not_by_category(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.sqlite")
    led.record(intervention(trigger="gate_retry", answer=None, fallback="timeout"))
    s = led.stats()
    assert s.by_trigger["gate_retry"] == (1, 0) and s.by_category == {} and s.total == 1


def test_recent_and_for_layer_are_newest_first(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "s.sqlite")
    ids = [led.record(intervention()) for _ in range(3)]
    assert [r.id for r in led.recent()] == ids[::-1]
    assert [r.id for r in led.recent(limit=2)] == ids[:0:-1]
    assert [r.id for r in led.for_layer("AIR-1", "01")] == ids[::-1]
    assert led.for_layer("AIR-1", "02") == []
