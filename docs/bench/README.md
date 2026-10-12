# Benchmarks

Every number here was produced through the `Engine` seam (`HTTPEngine`) against the managed vllm-mlx process started
by `task up`, on the Mac Studio M5 Max / 128 GB. Re-run with the commands shown; JSON next to each table is the raw
output (`task bench:compare -- a.json b.json` prints deltas).

## Method (`task bench`)

- Prompt = a random nonce **first**, then repository text, sized to the target token count (the server's reported
  `prompt_tokens` is the truth). The nonce defeats the prefix cache, so prefill is really measured.
- `N` identical-size requests are sent concurrently; `gen` tokens each at temperature 0, thinking off.
- `ttft_ms` = time to the first streamed token (includes prefill); `prefill_tps` = prompt tokens / TTFT;
  `decode_tps/stream` = generated tokens / (total − TTFT) per stream; `agg_decode_tps` = that × N.
- `resume_ttft_ratio` (N=1 cells) = TTFT of turn 2 of a conversation ÷ TTFT of turn 1, with the whole turn-1 prompt
  as shared prefix. Near 0 means the prefix cache resumed the conversation (spec 02 §3, 04 §2); near 1 means every
  turn re-prefills.
- `short` counts replies that stopped before 80 % of `gen` (a sign the measurement under-counts decode).

## Files

| File | Model | Notes |
| --- | --- | --- |
| `2026-10-12-engine-bench.md` | base vs qwen38 | tables, comparison and interpretation |
| `2026-10-12-lts-bench.md` / `lts-lang_detect.json` | never vs heuristic, K=3 | first `task bench:lts` run; verdict not meaningful yet (0 retrievals, 1 sample) |
| `qwen38.json` | mlx-community/Qwen3.8-27B-4bit | hybrid attention: no prefix cache in vllm-mlx 0.5.0 |
| `base.json` | mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit | default team model |
