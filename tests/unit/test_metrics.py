"""airlie.metrics: DuckDB aggregation over the event JSONL (metrics tab, layer 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.metrics import series, summary

FIXTURES = Path(__file__).parents[1] / "fixtures" / "metrics"


def _state_dir(tmp_path: Path, *files: str) -> Path:
    """A state dir with one run and one story, each holding a copy of the named fixture file(s)."""
    for name in files:
        data = (FIXTURES / name).read_text()
        for sub in ("runs", "stories"):
            d = tmp_path / sub / "x"
            d.mkdir(parents=True)
            (d / "events.jsonl").write_text(data)
    return tmp_path


def test_summary_new_format_splits_prefill_and_generation(tmp_path: Path) -> None:
    state = _state_dir(tmp_path, "events_new.jsonl")
    s = summary(state, 0.0, 1e12, "model")
    # 4 step events: prompt 2700 / prefill 135 ms, completion 900 / gen 270 ms
    assert s["prefill_tps"] == pytest.approx(2700 / 135 * 1000)
    assert s["generation_tps"] == pytest.approx(900 / 270 * 1000)
    assert s["stories"] == 1
    # the fixture is copied into both runs/ and stories/, so each event counts twice: 4 fix_rounds / 1 layer
    assert s["fix_rounds_per_layer"] == pytest.approx(4.0)
    assert set(s["by"]) == {"qwen3-coder", "muse-glimmer"}
    assert s["by"]["qwen3-coder"]["prefill_tps"] == pytest.approx(1500 / 75 * 1000)
    assert s["by"]["qwen3-coder"]["generation_tps"] == pytest.approx(300 / 150 * 1000)
    assert s["by"]["muse-glimmer"]["prefill_tps"] == pytest.approx(1200 / 60 * 1000)
    assert s["by"]["muse-glimmer"]["generation_tps"] == pytest.approx(600 / 120 * 1000)


def test_summary_old_format_prefill_unknown_and_no_row_dropped(tmp_path: Path) -> None:
    """AC2: a pre-change line with only gen_ms aggregates without exception or dropped row."""
    state = _state_dir(tmp_path, "events_old.jsonl")
    s = summary(state, 0.0, 1e12, "model")
    assert s["prefill_tps"] is None
    assert s["generation_tps"] == pytest.approx(180 / 180 * 1000)
    assert s["by"] == {"unknown": {"generation_tps": pytest.approx(1000.0), "prefill_tps": None}}


def test_summary_missing_slice_values_bucket_under_unknown(tmp_path: Path) -> None:
    state = _state_dir(tmp_path, "events_old.jsonl")
    s = summary(state, 0.0, 1e12, "role")
    assert set(s["by"]) == {"unknown"}
    assert s["by"]["unknown"]["generation_tps"] == pytest.approx(1000.0)


def test_summary_range_filters_out_of_range_events(tmp_path: Path) -> None:
    state = _state_dir(tmp_path, "events_new.jsonl")
    s = summary(state, 1001.5, 1002.5, "model")
    # only the muse-glimmer step at ts 1002 falls in range
    assert s["generation_tps"] == pytest.approx(400 / 80 * 1000)
    assert s["prefill_tps"] == pytest.approx(800 / 40 * 1000)
    assert set(s["by"]) == {"muse-glimmer"}


def test_series_buckets_and_quantiles_ordered(tmp_path: Path) -> None:
    state = _state_dir(tmp_path, "events_new.jsonl")
    rows = series(state, "generation_tps", "model", 0.0, 1e12, bucket_s=10)
    # one row per (bucket, slice value): all steps fall in one 10 s bucket, two models
    assert len(rows) == 2
    row = next(r for r in rows if r["model"] == "qwen3-coder")
    assert row["t"] == 1000.0
    for q in ("p10", "p50", "p90"):
        assert q in row
    assert row["p10"] <= row["p50"] <= row["p90"]


def test_series_empty_range_is_empty(tmp_path: Path) -> None:
    state = _state_dir(tmp_path, "events_new.jsonl")
    assert series(state, "generation_tps", "model", 5000.0, 6000.0, bucket_s=10) == []


def test_summary_empty_state_dir(tmp_path: Path) -> None:
    s = summary(tmp_path, 0.0, 1e12, "model")
    assert s["generation_tps"] is None
    assert s["prefill_tps"] is None
    assert s["stories"] == 0
    assert s["fix_rounds_per_layer"] is None
    assert s["by"] == {}
