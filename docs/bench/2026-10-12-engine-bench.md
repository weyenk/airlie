# Engine bench, 2026-10-12

Mac Studio M5 Max / 128 GB · vllm-mlx 0.5.0 via `task up` (`--continuous-batching --max-num-seqs 16 --max-kv-size 65536
--prefill-step-size 2048`, prefix cache on) · measured through `HTTPEngine` · `gen 256`, temperature 0, thinking off,
one run per cell. Raw data: `base.json`, `qwen38.json`. Regenerate the tables with `task bench:compare`/`airlie bench report`.

## base = mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit (MoE, standard attention; default team model)

| model | ctx   | N | ttft_ms | prefill_tps | decode_tps/stream | agg_decode_tps | prompt_tok | gen_tok | short | resume_ttft_ratio |
|-------|-------|---|---------|-------------|-------------------|----------------|------------|---------|-------|-------------------|
| base  | 4000  | 1 | 992.5   | 4157.3      | 112.0             | 112.0          | 4126       | 256     | 0     | 0.1               |
| base  | 4000  | 2 | 2597.1  | 1651.7      | 78.2              | 156.4          | 8579       | 512     | 0     | -                 |
| base  | 4000  | 4 | 4253.4  | 967.4       | 51.3              | 205.3          | 16459      | 1024    | 0     | -                 |
| base  | 32000 | 1 | 16323.5 | 2118.2      | 54.3              | 54.3           | 34577      | 256     | 0     | 0.0               |
| base  | 32000 | 2 | 31413.3 | 1072.7      | 33.4              | 66.8           | 67394      | 512     | 0     | -                 |
| base  | 32000 | 4 | 61009.3 | 541.9       | 9.7               | 39.0           | 132254     | 1024    | 0     | -                 |

## qwen38 = mlx-community/Qwen3.8-27B-4bit (dense hybrid attention; catalog only)

| model  | ctx   | N | ttft_ms  | prefill_tps | decode_tps/stream | agg_decode_tps | prompt_tok | gen_tok | short | resume_ttft_ratio |
|--------|-------|---|----------|-------------|-------------------|----------------|------------|---------|-------|-------------------|
| qwen38 | 4000  | 1 | 4766.4   | 927.5       | 25.7              | 25.7           | 4421       | 256     | 0     | 1.0               |
| qwen38 | 4000  | 2 | 9367.5   | 483.9       | 25.6              | 51.2           | 9065       | 341     | 1     | -                 |
| qwen38 | 4000  | 4 | 17986.4  | 242.9       | 23.2              | 92.7           | 17472      | 853     | 1     | -                 |
| qwen38 | 32000 | 1 | 41967.9  | 865.9       | 17.0              | 17.0           | 36341      | 256     | 0     | 1.0               |
| qwen38 | 32000 | 2 | 61763.9  | 576.1       | 8.1               | 16.2           | 71159      | 512     | 0     | -                 |
| qwen38 | 32000 | 4 | 120427.1 | 289.9       | 4.6               | 18.5           | 139643     | 1024    | 0     | -                 |

## base relative to qwen38 (per cell, %)

| ctx   | concurrency | a      | b    | ttft_ms_delta_pct | decode_tps_per_stream_delta_pct | agg_decode_tps_delta_pct | prefill_tps_delta_pct |
|-------|-------------|--------|------|-------------------|---------------------------------|--------------------------|-----------------------|
| 4000  | 1           | qwen38 | base | -79.2             | 336.5                           | 336.5                    | 348.2                 |
| 4000  | 2           | qwen38 | base | -72.3             | 205.3                           | 205.3                    | 241.3                 |
| 4000  | 4           | qwen38 | base | -76.4             | 121.5                           | 121.5                    | 298.3                 |
| 32000 | 1           | qwen38 | base | -61.1             | 219.2                           | 219.2                    | 144.6                 |
| 32000 | 2           | qwen38 | base | -49.1             | 313.4                           | 313.2                    | 86.2                  |
| 32000 | 4           | qwen38 | base | -49.3             | 110.4                           | 110.5                    | 86.9                  |

## Reading the numbers

- **Prefix cache (spec 02 §3, 04 §2).** `resume_ttft_ratio` is turn-2 TTFT ÷ turn-1 TTFT with the whole turn-1 prompt
  as shared prefix. base: **0.1 at 4k, 0.0 at 32k** (turn 2 pays only the new tokens). qwen38: **1.0** at both sizes,
  every turn re-prefills the entire conversation, because vllm-mlx 0.5.0 cannot store hybrid-attention KV state
  (`memory_cache.py`: "SSM state can't be rewound"). This alone decides the team model.
- **Prefill.** base 4.2k tok/s at 4k and 2.1k tok/s at 32k single-stream; qwen38 0.9k tok/s. A fresh 32k-token prompt
  costs 16 s on base versus 42 s on qwen38, and with the cache a continuing conversation costs far less than either.
- **Decode.** base 112 tok/s single-stream at 4k (qwen38: 26), 54 tok/s at 32k (qwen38: 17). Aggregate decode scales
  to 205 tok/s at N=4/4k (1.8× single), but at N=4/32k per-stream decode falls to 9.7 tok/s (132k tokens of KV in
  flight): long-context concurrency is KV-bandwidth bound. For K=3 teams the realistic working regime is ≤32k context
  per team with 33–54 tok/s per stream.
- **Engine ceiling observed.** Peak Metal memory during the 32k×4 cell: ~30.5 GB active (`[Metal memory]` log line),
  within the 80 GB projection.
- Not measured yet: `--enable-mtp` (M6), KV quantization, `llama70b` / `qwen25_32b` (not downloaded; run
  `task models:pull -- llama70b qwen25_32b` then `task bench:models`).
