# Benchmarks

Every number here was produced through the `Engine` seam (`HTTPEngine`) against the managed engine process started
by `task up` (vllm-mlx until the move to mlx-vlm, 2026-11; the tables below predate it), on the Mac Studio M5 Max / 128 GB. Re-run with the commands shown; JSON next to each table is the raw
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

## Output parsing without server parser flags (checked live 2026-10-13; historical)

This section describes vllm-mlx 0.5.0 and airlie's client-side parser (`engine/output_parse.py`), both removed
with the move to mlx-vlm, which parses tool calls and reasoning on the server.

A throwaway `vllm-mlx serve` of Qwen3-4B with no `--enable-auto-tool-choice` and no `--reasoning-parser` was
sent a chat request with one `read_file` tool: the tools were rendered into the prompt, the model answered with a
call, `finish_reason` was `tool_calls`. vllm-mlx renders `tools` whenever the request carries them (the parser
flag only chooses the parser), and without a flag it applies a generic fallback that recognises JSON-in-
`<tool_call>`, Llama `<function=…>`, MiniMax and Nemotron styles. Checked the same day with the coder model
(Qwen3-Coder-30B-A3B) on a throwaway no-parser server: its `<tool_call><function=…><parameter=…>` XML matches
the fallback's Nemotron pattern, so the server parsed it too and `HTTPEngine` received a `ToolCall` with or
without the client parser. What does come back raw is Harmony (gpt-oss) and Muse Glimmer's channels; those are
what `airlie.engine.output_parse` parses on the client with vllm-mlx's own parser classes
(`engines.main.server_parsers: false`), validated on fixtures until Muse is pulled in plan 2. Server-parsed calls
always win; nothing is parsed twice. **But** (2026-10-13, studio bench attempt 1): the *managed* engine
(`--models-config`, continuous batching) started without the parser flags rendered tools in vllm-mlx's own
bracket protocol (`[Calling tool: name({...})]`) and the coder's calls came back with empty arguments; the
single-agent baseline failed in 6 s. The studio profile therefore keeps `server_parsers: true`; client-side
parsing is for what the server leaves raw.

## Routing (Kev-4B on llama-server, `task route:eval`, 2026-10-13; historical)

`ggml-org/Kev-4B-GGUF` (Q4_K_M, 3 GB) behind `/v1/systemone`, asked the role question over the nine criteria in
`evals/routing-roles.yaml` for the 54 labelled tasks in `evals/routing.jsonl` (ai-mode's classify sets, role
rows only): **54/54 correct (accuracy 1.00), mean confidence
0.64, wrong-but-clear 0**, no confusions. Raw: `routing.json`.
The design's bar for using studio routing for real work is accuracy ≥ 0.9. Mean confidence 0.64 means the
`clear / ambiguous / low-confidence` policy (gap 0.15, floor 0.5) will route a fair share of tasks through the
chief's hint; the per-decision log (`runs/<id>/routing.jsonl`) is what to tune the criteria lines against.

## Studio topology (2026-10-13)

`task bench:topology -- --topologies single,studio …` on the studio profile: studio 4/6 vs single 5/6; solved
wall-clock 441 s vs 60 s; 9 reviews (4 request_changes, 2 approve, 3 unparsable); routing 10/10 as hinted. The
design's original criterion is not met; the per-run analysis and the two defects it exposed are in
`2026-10-13-topology-bench.md` ("Studio"). Raw: `topology-studio.json`, `routing.json`. Noah's call
(2026-10-13): studio is a different product (plan, tests, docs, review), so the single agent is a reference
column, not a bar; studio is benched on multi-role tasks (`studio_feature`: 1 of 2 passed, the miss traced to
the delegate bug fixed the same day), and `single` stays the default topology.

## Story bench (`task bench:story`, from 2026-10-26)

A fixed request built end to end by the story pipeline, on a state directory of its own (no lessons, board or
runs of an earlier run), so a pipeline change is read against the same work. The cases are in `evals/stories/`;
`roman` (a 51-line module in weyenk/airlie-sandbox with an exact answer) is the first. A run holds the engine
for hours, so it runs on demand, never in CI:

```bash
task bench:story -- roman --baseline docs/bench/story-roman-<earlier>.json
```

It records `story-<case>-<time>.json` here: what the run cost (the phases, tokens and tokens per second by role
and phase, the retries: meeting turns and objections, package attempts, critic objections, gate re-plans,
supervisor triages) and how good its top branch is (its own gate, the case's oracle for the written spec, the
mutation score of its own tests, its tests and assertions). It closes the PRs it published and deletes their
branches, and prints the case's budgets as regression lines and, with `--baseline`, the deltas
(`task bench:story-compare -- a.json b.json` for two saved records).

### The reliability loop (`task bench:sweep`, from 2026-11-06)

Four real stories rerun from scratch, again and again, until each goes through first try, with every fix
checked against all four so none gets worse: `air55`, `air59`, `air61` and `air62` in `evals/stories/`, each the
card's title and description as they read on 2026-11-06 (the title is the request's first line).

```bash
task bench:sweep -- air55 air59 air61 air62
```

runs them one after another (hours each: it holds the engine), writes each record here, and prints a row per case
(`first_try`, wall minutes, package tries, layer builds, blocks, gate failures, and the previous record's
`first_try`) and then each case's comparison with its previous record in this directory (the latest
`story-<case>-<time>.json`; a hand-named baseline is not one). A case that cannot start (its clone, its config)
is a row of its own and the sweep goes on; it exits 1 when any case did not go through first try.

What the four cases set, and any case can:

- `publish: false` (or `--no-publish` on `bench:story` and `bench:sweep`): the story runs with
  `story.publish: none`. It ends once every layer is built and gated (or satisfied): nothing is pushed, no PR
  is opened, and GitHub is never asked, not even for the bot's token or identity; the only network call is a
  fetch from `origin` when the pinned base is not in the clone yet. The record says `"publish": false`, and the
  run's local branches and kept worktrees are deleted after it as a published run's are.
- `pin_base: true`: the story builds on `base` itself (`c0399cd`, main at #244, before any of the four merged),
  not on the remote's trunk as it is when the run starts, so every run builds on the same code.
- `clone:`: the checkout the story is built in. A case in `weyenk/airlie` would otherwise resolve to
  `story.repos_root/airlie`, which is the home checkout serve runs from; these cases (and `wiki`) use
  `~/.local/state/airlie-bench/repos/airlie`, cloned from GitHub over HTTPS the first time. The bench refuses a
  clone that is the checkout it runs from (or a worktree of it), or whose `origin` names another repository.
- `oracle: null`: there is no exact answer, so `quality` is the top branch's gate and the tests each layer's
  package wrote (`layer_tests`) only.

Every record (any case) now carries `first_try` and `tries`, read from the story's events and its studio runs':
per layer the package tries (a drafted package's try counts too), the kinds of each `package_returned`, the
builds, why each re-plan was asked and the gate failures inside its builds; story-wide those summed, the
blocks and the steering events (`answer_after_cuts`, `last_turn_tool_call`). `first_try` is true when every
layer was packaged on try 1 (none returned), built on build 1 (no re-plan, no gate failure) or satisfied, the
story never blocked and it reached review; `tries.why_not` says what kept it from that, and a case whose
budgets say `first_try: true` prints those lines as its verdict. `bench:story-compare` shows `first_try` and
each count side by side, changed or not.

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
| `2026-10-14-qwen3-coder-tool-calls.md` | base (Qwen3-Coder) and the Qwen3 XML tool format | why tool calls arrived corrupted (vllm-mlx 0.5.0's `qwen3_coder` parser + airlie's stream assembly), the replayed options, and the `qwen3_xml_raw` fix |
| `2026-10-12-engine-bench.md` | base vs qwen38 | tables, comparison and interpretation |
| `2026-10-13-topology-bench.md` / `topology.json` | single vs K=3 teams vs fork-when-stuck, 3 tasks × 3 repeats, native loop | single matches K=3 on pass rate at 45 % of the wall-clock; fork loses (trigger too early, best-effort picks quitters); default → single |
| `2026-10-12-lts-bench-bigger-v2.md` / `lts-bigger-v2.json` | round 2: background admission + ranked injection, 3 repeats | admission wait gone, still +28 % per step from the injected block; 0 retrievals; runs dominated by solve/no-solve; criterion not met |
| `2026-10-12-lts-bench-bigger.md` / `lts-bigger.json` | never vs heuristic on inventory_cli + log_pipeline, K=3 | still 0 retrievals; LTS overhead doubled wall-clock; criterion not met |
| `2026-10-12-lts-bench.md` / `lts-lang_detect.json` | never vs heuristic, K=3 | first `task bench:lts` run; verdict not meaningful yet (0 retrievals, 1 sample) |
| `qwen38.json` | mlx-community/Qwen3.8-27B-4bit | hybrid attention: no prefix cache in vllm-mlx 0.5.0 (measured on the old engine) |
| `base.json` | mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit | default team model |
