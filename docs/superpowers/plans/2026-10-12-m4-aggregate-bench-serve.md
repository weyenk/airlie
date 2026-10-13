# M4 Aggregation, LTS Benchmark, Serve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `task run` returns one aggregated answer and a winning branch (spec 01 §4 step 9 LLM aggregator);
`task bench:lts` reports the spec 04 §4 criterion (≥25 % wall-clock reduction vs the memory-free baseline);
`task serve` exposes airlie to any client over the OpenAI API.

**Spec:** `01_…` §4 step 9 (LLM aggregator), `04_…` §4 (benchmark criterion), §3 (K teams via `execute_task`).

## Tasks
1. `teams/aggregate.py`: `Aggregator` protocol; `TestScoreAggregator` (runs `test_cmd` per worktree, picks the best,
   ties by fewest steps); `LLMAggregator` (role `aggregator` sees every team's final text + test output, names the
   winning team and writes the final verified answer; falls back to test score if it names no team). Orchestrator
   runs it after the teams, writes `runs/<id>/report.md`, emits `aggregate`.
2. `bench/lts_bench.py` + `airlie bench lts`: tasks × controllers (`never` baseline vs configured) × repeats →
   wall-clock, steps, admissions, retrievals, hidden-test pass rate; verdict on the ≥25 % criterion; JSON + table.
3. `serve/app.py` + `airlie serve`: `/v1/models` (roles), `/v1/chat/completions` proxied to the engine with role →
   served model mapping (streaming passthrough), `/v1/airlie/run` (goal, repo, k → report JSON).
4. `stop_on_first_pass`: when a team's `test_cmd` passes, cancel the others (configurable).
5. README, Taskfile (`run` already; add `bench:lts`, `serve`); live `task bench:lts` on lang_detect.
