"""DuckDB aggregation over the event JSONL: the metrics tab's performance core.

Reads state_dir/runs/*/events.jsonl and state_dir/stories/*/events.jsonl directly (no cache in v1:
the files stay the source of truth) and answers summary() and series() over a time range, split
by any slice field (model, role, topology, story). A step event written before the prefill split
(only gen_ms, no prefill_ms, no model/role/topology) aggregates without exception or dropped row:
its prefill renders as 'unknown' (None in the dict) and missing slice values bucket under
'unknown'.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb  # noqa: F401  (the module's declared engine; the v1 aggregation runs in-process)

_SLICE_FIELDS = ("model", "role", "topology", "story")
# the slices the API offers that are named differently in the step event: the task is the run a step
# belongs to (`run_id`), the agent is the team inside it that took the step (`team_id`)
_SLICE_SOURCES = {"task": "run_id", "agent": "team_id"}


def _event_files(state_dir: Path) -> list[Path]:
    files = sorted((state_dir / "runs").glob("*/events.jsonl"))
    files += sorted((state_dir / "stories").glob("*/events.jsonl"))
    return [f for f in files if f.is_file()]


def _quantile(values: list[float], q: float) -> float:
    """Linear-interpolated quantile of a non-empty list (numpy-free, matches numpy's default)."""
    vs = sorted(values)
    pos = (len(vs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    frac = pos - lo
    return vs[lo] * (1 - frac) + vs[hi] * frac


def _row(rec: dict[str, Any]) -> dict[str, Any]:
    """One event line as a flat row: the fields we aggregate on, or None when absent."""
    out: dict[str, Any] = {
        "ts": rec.get("ts"),
        "kind": rec.get("kind"),
        "story": rec.get("story"),
        "layer": rec.get("layer"),
    }
    for f in _SLICE_FIELDS:
        v = rec.get(f)
        out[f] = "unknown" if v is None else str(v)
    for name, source in _SLICE_SOURCES.items():
        v = rec.get(source)
        out[name] = "unknown" if v is None else str(v)
    for f in ("prompt_tokens", "completion_tokens", "prefill_ms", "gen_ms"):
        v = rec.get(f)
        out[f] = float(v) if isinstance(v, (int, float)) and v is not None else None
    return out


def _rows(state_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in _event_files(state_dir):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn final line is not a row; it is never counted
                if not isinstance(rec, dict):
                    continue
                rows.append(_row(rec))
    return rows


def _in_range(rows: list[dict[str, Any]], from_ts: float, to_ts: float) -> list[dict[str, Any]]:
    return [r for r in rows if isinstance(r["ts"], (int, float)) and from_ts <= r["ts"] < to_ts]


def _tps(numer: list[float | None], denom: list[float | None]) -> float | None:
    """sum(numer)/sum(denom)*1000, or None when the denominator is absent or zero."""
    d = sum(v for v in denom if v)
    if not d:
        return None
    return sum(v for v in numer if v) / d * 1000


def summary(state_dir: Path, from_ts: float, to_ts: float, slice_by: str) -> dict[str, Any]:
    """Aggregate the step events in [from_ts, to_ts) into one dict.

    Keys: generation_tps, prefill_tps (None when no prefill_ms was recorded), stories (distinct
    story keys in range), fix_rounds_per_layer (fix_round events / distinct layers, None when
    there are no layers), and by: one entry per slice value present in range, each with
    generation_tps and prefill_tps.
    """
    rows = _in_range(_rows(state_dir), from_ts, to_ts)
    steps = [r for r in rows if r["kind"] == "step"]

    by: dict[str, Any] = {}
    for v in sorted({r[slice_by] for r in steps}):
        sub = [r for r in steps if r[slice_by] == v]
        by[v] = {
            "generation_tps": _tps([r["completion_tokens"] for r in sub], [r["gen_ms"] for r in sub]),
            "prefill_tps": _tps([r["prompt_tokens"] for r in sub], [r["prefill_ms"] for r in sub]),
        }

    stories = {r["story"] for r in rows if r["story"]}
    layers = {r["layer"] for r in rows if r["layer"] is not None}
    fix_rounds = sum(1 for r in rows if r["kind"] == "fix_round")
    return {
        "generation_tps": _tps([r["completion_tokens"] for r in steps], [r["gen_ms"] for r in steps]),
        "prefill_tps": _tps([r["prompt_tokens"] for r in steps], [r["prefill_ms"] for r in steps]),
        "stories": len(stories),
        "fix_rounds_per_layer": fix_rounds / len(layers) if layers else None,
        "by": by,
    }


def series(
    state_dir: Path, metric: str, slice_by: str, from_ts: float, to_ts: float, bucket_s: float
) -> list[dict[str, Any]]:
    """One row per bucket width in [from_ts, to_ts): {t, <slice>, p10, p50, p90}.

    `metric` is a per-step rate: 'generation_tps' (completion_tokens/gen_ms*1000) or
    'prefill_tps' (prompt_tokens/prefill_ms*1000). Buckets with no steps for a slice value are
    omitted; quantiles are linear-interpolated so p10 <= p50 <= p90.
    """
    if metric not in ("generation_tps", "prefill_tps"):
        raise ValueError(f"unknown metric {metric!r}: expected 'generation_tps' or 'prefill_tps'")
    rows = _in_range(_rows(state_dir), from_ts, to_ts)
    steps = [r for r in rows if r["kind"] == "step"]

    def rate(r: dict[str, Any]) -> float | None:
        if metric == "generation_tps":
            num, den = r["completion_tokens"], r["gen_ms"]
        else:
            num, den = r["prompt_tokens"], r["prefill_ms"]
        if num is None or not den:
            return None
        return float(num / den * 1000)

    buckets: dict[tuple[float, str], list[float]] = {}
    for r in steps:
        t = float(r["ts"])
        key = (from_ts + (t - from_ts) // bucket_s * bucket_s, r[slice_by])
        v = rate(r)
        if v is not None:
            buckets.setdefault(key, []).append(v)

    out = [
        {
            "t": t,
            slice_by: v,
            "p10": _quantile(vals, 0.1),
            "p50": _quantile(vals, 0.5),
            "p90": _quantile(vals, 0.9),
        }
        for (t, v), vals in sorted(buckets.items())
    ]
    return out
