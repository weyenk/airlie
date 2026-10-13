# Benchmarks

Every number here was produced through the `Engine` seam (`HTTPEngine`) against the managed vllm-mlx process started
by `task up`, on the Mac Studio M5 Max / 128 GB. Re-run with the commands shown; JSON next to each table is the raw
output (`task bench:compare -- a.json b.json` prints deltas).

## Before any benchmark: restart the engine, and know the model's failure mode

Finding (2026-10-13): the base coder model (Qwen3-Coder-30B-A3B-4bit at temperature 0.3) sometimes collapses into
repetition mid-generation (a file's content turning into thousands of `0`s, or `run_tests` call blocks repeated
until `max_tokens`). Server log signature: `Chat completion: 8192 tokens in 200–400s`. It happened in 121 of the
first ~600 completions on 2026-10-12/13 regardless of topology, so it is a model pathology, not a cache or
topology effect; a day-old prefix cache made the argument-less-call variant worse and `task restart` cleared that
part. Guards now in `airlie.yaml`: `roles.coder.sampling.repetition_penalty: 1.05` (sent per request on both
team paths) and `roles.coder.max_tokens: 4096` (bounds a runaway to ~2 min); a cut-off tool call is reported to
the model as such instead of executed. Still: run `task restart` before a benchmark, and compare the count of
`8192 tokens` / `4096 tokens` completions in `engines/main/server.log` across runs when results look off.

## Output parsing without server parser flags (checked live 2026-10-13)

A throwaway `vllm-mlx serve` of Qwen3-4B with no `--enable-auto-tool-choice` and no `--reasoning-parser` was
sent a chat request with one `read_file` tool: the tools were rendered into the prompt, the model answered with a
call, `finish_reason` was `tool_calls`. vllm-mlx renders `tools` whenever the request carries them (the parser
flag only chooses the parser), and without a flag it applies a generic fallback that recognises JSON-in-
`<tool_call>`, Llama `<function=…>`, MiniMax and Nemotron styles. Qwen3-Coder's XML, Harmony and Muse Glimmer's
channels come back raw, which is what `airlie.engine.output_parse` parses on the client with vllm-mlx's own
parser classes (`engines.main.server_parsers: false`). Server-parsed calls always win; nothing is parsed twice.

## Routing (Kev-4B on llama-server, `task route:eval`, 2026-10-13)

`ggml-org/Kev-4B-GGUF` (Q4_K_M, 3 GB) behind `/v1/systemone`, asked the role question over the nine criteria in
`evals/routing-roles.yaml` for the 54 labelled tasks in `evals/routing.jsonl` (ai-mode's classify sets, role
rows only): **54/54 correct (accuracy 1.00), mean confidence
0.64, wrong-but-clear 0**, no confusions. Raw: `routing.json`.
The design's bar for using studio routing for real work is accuracy ≥ 0.9. Mean confidence 0.64 means the
`clear / ambiguous / low-confidence` policy (gap 0.15, floor 0.5) will route a fair share of tasks through the
chief's hint; the per-decision log (`runs/<id>/routing.jsonl`) is what to tune the criteria lines against.

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
| `2026-10-13-topology-bench.md` / `topology.json` | single vs K=3 teams vs fork-when-stuck, 3 tasks × 3 repeats, native loop | single matches K=3 on pass rate at 45 % of the wall-clock; fork loses (trigger too early, best-effort picks quitters); default → single |
| `2026-10-12-lts-bench-bigger-v2.md` / `lts-bigger-v2.json` | round 2: background admission + ranked injection, 3 repeats | admission wait gone, still +28 % per step from the injected block; 0 retrievals; runs dominated by solve/no-solve; criterion not met |
| `2026-10-12-lts-bench-bigger.md` / `lts-bigger.json` | never vs heuristic on inventory_cli + log_pipeline, K=3 | still 0 retrievals; LTS overhead doubled wall-clock; criterion not met |
| `2026-10-12-lts-bench.md` / `lts-lang_detect.json` | never vs heuristic, K=3 | first `task bench:lts` run; verdict not meaningful yet (0 retrievals, 1 sample) |
| `qwen38.json` | mlx-community/Qwen3.8-27B-4bit | hybrid attention: no prefix cache in vllm-mlx 0.5.0 |
| `base.json` | mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit | default team model |
