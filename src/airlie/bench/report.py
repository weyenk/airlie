"""Plain-text tables for bench results (also pasted into docs/bench/*.md)."""

from __future__ import annotations

from typing import Any

from airlie.bench.engine_bench import BenchResult


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def render_table(results: list[BenchResult]) -> str:
    cols = [
        "model",
        "ctx",
        "N",
        "ttft_ms",
        "prefill_tps",
        "decode_tps/stream",
        "agg_decode_tps",
        "prompt_tok",
        "gen_tok",
        "short",
        "resume_ttft_ratio",
    ]
    rows = []
    for r in results:
        rows.append(
            [
                r.model,
                r.ctx,
                r.concurrency,
                r.ttft_ms,
                r.prefill_tps,
                r.decode_tps_per_stream,
                r.agg_decode_tps,
                r.prompt_tokens,
                r.completion_tokens,
                r.short_outputs,
                (r.resume.ttft_ratio if r.resume else None),
            ]
        )
    widths = [
        max(len(c), *(len(_fmt(row[i])) for row in rows)) if rows else len(c) for i, c in enumerate(cols)
    ]
    line = "| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = [
        "| " + " | ".join(_fmt(v).ljust(w) for v, w in zip(row, widths, strict=True)) + " |" for row in rows
    ]
    return "\n".join([line, sep, *body])


def render_compare(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no overlapping cases)"
    cols = list(rows[0].keys())
    widths = [max(len(c), *(len(_fmt(r[c])) for r in rows)) for c in cols]
    line = "| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = [
        "| " + " | ".join(_fmt(r[c]).ljust(w) for c, w in zip(cols, widths, strict=True)) + " |" for r in rows
    ]
    return "\n".join([line, sep, *body])
