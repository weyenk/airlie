"""Engine bench: deterministic prompts, concurrency matrix, resume measurement, JSON + table report."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.bench.engine_bench import BenchCase, BenchResult, build_prompt, compare, run_case, run_matrix
from airlie.bench.report import render_table
from airlie.config import Sampling
from airlie.engine import FakeEngine, FakeReply


def test_build_prompt_starts_with_nonce_and_targets_token_count(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n" * 200)
    p1 = build_prompt(400, seed=1, corpus_dir=tmp_path)
    p2 = build_prompt(400, seed=2, corpus_dir=tmp_path)
    assert p1.split()[0] != p2.split()[0]  # nonce first, so prefix caches cannot fake prefill
    assert 1200 <= len(p1) <= 2400  # ~4 chars/token estimate within 25 %


def test_build_prompt_without_corpus_uses_synthetic_text(tmp_path: Path) -> None:
    assert len(build_prompt(100, seed=1, corpus_dir=tmp_path / "none")) >= 300


async def test_run_case_reports_aggregate_and_per_stream_numbers() -> None:
    eng = FakeEngine({"coder": [FakeReply(text="w " * 40)] * 3})
    res = await run_case(
        eng, BenchCase(role="coder", ctx=100, concurrency=3, gen=40), sampling=Sampling(), corpus_dir=None
    )
    assert res.concurrency == 3 and res.completion_tokens == 120
    assert res.agg_decode_tps > 0 and res.decode_tps_per_stream > 0
    assert res.ttft_ms >= 0


async def test_run_matrix_covers_every_ctx_and_concurrency() -> None:
    eng = FakeEngine({"coder": [FakeReply(text="x y z")] * 100})
    results = await run_matrix(
        eng, role="coder", ctxs=[50, 100], ns=[1, 2], gen=3, runs=1, sampling=Sampling(), corpus_dir=None
    )
    assert sorted((r.ctx, r.concurrency) for r in results) == [(50, 1), (50, 2), (100, 1), (100, 2)]


async def test_resume_case_measures_two_turns() -> None:
    eng = FakeEngine({"coder": [FakeReply(text="ok"), FakeReply(text="ok again")]})
    res = await run_case(
        eng,
        BenchCase(role="coder", ctx=100, concurrency=1, gen=4, resume=True),
        sampling=Sampling(),
        corpus_dir=None,
    )
    assert res.resume is not None
    assert res.resume.turn2_prompt_tokens > res.resume.turn1_prompt_tokens
    assert len(eng.requests[1].messages) == len(eng.requests[0].messages) + 2


def test_render_and_compare(tmp_path: Path) -> None:
    a = [
        BenchResult(
            model="m",
            role="coder",
            ctx=4000,
            concurrency=1,
            gen=64,
            runs=1,
            ttft_ms=100,
            prefill_tps=4000,
            decode_tps_per_stream=50,
            agg_decode_tps=50,
            prompt_tokens=4000,
            completion_tokens=64,
            rss_mb=None,
        )
    ]
    b = [
        BenchResult(
            model="n",
            role="coder",
            ctx=4000,
            concurrency=1,
            gen=64,
            runs=1,
            ttft_ms=50,
            prefill_tps=8000,
            decode_tps_per_stream=60,
            agg_decode_tps=60,
            prompt_tokens=4000,
            completion_tokens=64,
            rss_mb=None,
        )
    ]
    (tmp_path / "a.json").write_text(json.dumps([r.to_dict() for r in a]))
    (tmp_path / "b.json").write_text(json.dumps([r.to_dict() for r in b]))
    text = render_table(a)
    assert "ctx" in text and "4000" in text
    deltas = compare(tmp_path / "a.json", tmp_path / "b.json")
    assert deltas[0]["decode_tps_per_stream_delta_pct"] == 20.0
    assert deltas[0]["ttft_ms_delta_pct"] == -50.0


def test_load_results_round_trips_resume(tmp_path: Path) -> None:
    from airlie.bench.engine_bench import ResumeResult, load_results, save

    r = BenchResult(
        model="m",
        role="coder",
        ctx=4000,
        concurrency=1,
        gen=64,
        runs=1,
        ttft_ms=100,
        prefill_tps=4000,
        decode_tps_per_stream=50,
        agg_decode_tps=50,
        prompt_tokens=4000,
        completion_tokens=64,
        rss_mb=None,
        resume=ResumeResult(4000, 100.0, 4010, 8.0),
    )
    path = save([r], tmp_path / "r.json")
    back = load_results(path)
    assert back[0].resume is not None and back[0].resume.ttft_ratio == 0.08
    assert "0.1" in render_table(back)
