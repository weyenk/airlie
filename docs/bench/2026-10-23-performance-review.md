# Performance review, 2026-10-23

A read of the codebase at `644c3cc` against the run traces on this Mac (`~/.local/state/airlie`: 68 studio
runs since 2026-10-19, 2,099 step events; stories AIR-16, AIR-17, AIR-28, AIR-30, AIR-31; the engine's
`server.log`, 1,402 `[mllm_cache]` lines; the supervisor ledger). Numbers are from those traces unless a line
says it was measured here. Nothing was changed. Ranked by wall-clock per story layer, which is what Noah
measures a configuration by.

## Where the time goes

A story layer today is 30 to 75 minutes. Two clean recent layers (AIR-17 layers 01 and 02, runs
`20261023-172348-c85e` and `20261023-180116-a146`):

| part of the layer | c85e (37.4 min) | a146 (31.1 min) | what it is |
| --- | --- | --- | --- |
| tester, 14–16 steps | 31.8 min | 25.4 min | thinking Qwen3.8-27B at 17 tok/s; 5–6 steps hit the 4,096 thinking budget (220–300 s each) |
| tester's final reply | 3.4 min | 1.8 min | the no-tool "done" turn, thinking again; not a `step` event |
| pre-code check | 1.9 min | 2.2 min | ruff fix + check, then the **whole** suite with `-rA` |
| coder, 4–5 steps | 1.0 min | 0.5 min | non-thinking; 7 s a step |
| review (Muse) | 0.7 min | 1.0 min | approve, one call |
| final gate | 2.0 min | 1.8 min | `uv sync`, ruff ×2, mypy, the whole suite again |

Across every step since 2026-10-19 (what the `step` events record; a tool-call-only reply lands in
`prefill_ms`, see Observability below, so decode is if anything under-counted):

| | seconds | share |
| --- | --- | --- |
| generation (decode) | 57,461 | 79 % |
| prefill (TTFT) | 10,116 | 14 % |
| tools | 4,785 | 7 % |

By role, same window:

| role | steps | gen s/step | decode tok/s | median prompt | steps at the thinking budget | share of the role's gen time in those steps |
| --- | --- | --- | --- | --- | --- | --- |
| tester (thinking, budget 4,096) | 472 | 87 | 17.3 | 26k | 110 (23 %) | 69 % (260 s each; 35 s otherwise) |
| architect (thinking, budget 8,192) | 113 | 47 | 17.9 | 16k | 6 (5 %) | 49 % (433 s each; 25 s otherwise) |
| coder (no thinking) | 467 | 0.8 (+6 s TTFT) | n/a | 27k | – | – |
| product (no thinking) | 40 | 1.6 | 31 | 20k | – | – |

The tester alone is 11.4 hours of the 16 hours of decode in that window. The engine serves one sequence at a
time for nearly the whole of a story (survey ∥ refine is the only overlap), and per-stream decode falls with
context: 25.7 tok/s at 4k (engine bench), 17 at 26–30k (above), 10.8 tok/s on a 13k-token completion at
50k+ (`server.log`). Prefix-cache hit rate is 85 % (1,191 HIT / 211 MISS); the misses are the ones below.

## Findings, ranked

### 1. Decode of the thinking roles is three quarters of a layer, and it is concentrated in budget-hit steps

**Evidence.** 23 % of tester steps run to the 4,096-token thinking cap and take 69 % of the tester's time.
In c85e the steps that hit the cap each produced one `grep` or `read_file` (4,195–4,370 tokens of reasoning
to choose a read), then thought again on the next step. 14 `reasoning_carried` events in the window show
the cut-and-carry path firing; it does not stop the next step from thinking to the cap again (a146: three
consecutive cap hits at 7.8, 12.1 and 15.4 min). The completion-size distribution in `server.log` (last
2,000 completions): p50 99 tokens, p90 2,620, p99 8,343, max 16,384; completions ≥ 8k tokens are ~80 % of
engine time. Five completions ran 13–16k tokens at 10–14 tok/s: 10 to 20 minutes each, the 16,384 ones cut
off with nothing usable.

**Levers, cheapest first.**

- **Speculative decoding on the generalist.** The weights carry `mtp_num_hidden_layers: 1`, but
  mlx-community's quant drops the `mtp.` tensors (mlx-lm's `qwen3_5.py` sanitize strips them, line 350).
  vllm-mlx 0.5.0 supports MTP for this architecture (`patches/qwen3_5_mtp.py`, "Supports Dense (27B)") and
  its MLLM batched path (`mllm_batch_generator.install_mtp_mllm`), from a `model-mtp.safetensors` beside
  the quant, made by `scripts/add_mtp_weights_qwen35.py` in the vllm-mlx repository from the original
  checkpoint. On the MLLM path it drafts one token per verify step (cli.py:339), so the ceiling is 2× and
  the realistic gain 1.3–1.6× on every decoded token, which is 79 % of wall. Flags: `--enable-mtp
  --mtp-num-draft-tokens 1` via `engines.main.extra_args`; `/v1/status` reports attempts and acceptance.
  Check it against the hybrid prefix-cache patch: MTP snapshots and restores the cache, and the patch
  stores snapshots too. One layer run tells.
- **Thinking budget for the tester.** The cap is hit on 23 % of steps and those steps are 7× the others;
  2,048 halves the worst case. The carried tail (1,500 chars) is already how a cut step continues. Try one
  layer at 2,048 and one with `thinking: false` for the tester (the architect's brief is the plan, as the
  coder's change showed on 2026-10-18); keep whichever builds the layer in less wall-clock including fix
  rounds, which is Noah's criterion.
- **`max_tokens` on thinking roles.** 16,384 over a budget of 8,192 leaves 8k for an answer; a 16,384
  completion is 20 minutes of a dead turn. 12,288 caps the damage at ~5 minutes less with no change to
  what fits.
- **The seat, not the model.** The coder seat is cheap (7 s a step). The tester seat is where decode goes,
  so a seat-level choice (the MoE `coder8` for the tester: 3B active, standard attention, 60–113 tok/s in
  the earlier runs; or the generalist without thinking) is the one that changes the bill. That is a
  fidelity call for Noah; the point here is which seat the measurement should be about.

### 2. The engine runs one stream at a time; the aggregate ceiling is 3.6× higher

**Evidence.** The studio serialises writers (`write_lock`) and a story's subtasks depend on each other, so a
story is one request at a time apart from survey ∥ refine. `max_num_seqs` is 16. The engine bench measured
aggregate decode at N=4 of 92.7 tok/s against 25.7 single-stream on Qwen3.8 (3.6×); the one-story flock
(`story/state.py:story_lock`) keeps the second story out.

**What parallel stories need first** (Noah's plan, memory 2026-10-22), else they will be slower than
serial:

- **`--chunked-prefill-tokens`** (default 0, off): a 25k-token prefill takes ~30 s on the hybrid (prefill
  ~900 tok/s) and freezes every other stream's decode for that long. Every fresh continuation and every
  new subtask is such a prefill. 2,048–4,096 tokens per scheduler step.
- **Prefix-cache memory.** `MemoryAwarePrefixCache` gets 20 % of available RAM (9–20 GB in the log,
  varying with what else is resident) and the hybrid patch stores a snapshot on *every* request (`stored=`
  on each `[mllm_cache]` line). A 30k-token snapshot of the generalist is ~2 GB (16 full-attention layers ×
  4 KV heads × 256 dims × K,V × bf16 = 64 KB/token). One conversation leaves 10–20 snapshots in LRU; two
  or three conversations at once will evict each other's live prefix and re-prefill 25k tokens a step.
  Set `--cache-memory-mb` explicitly (the doctor's budget minus weights and live KV), and turn on
  `--kv-cache-quantization` (8-bit, applies to stored entries) to double what fits. `--prefix-trie-cache`
  is for the SimpleEngine only; not an option here.
- **Overlap inside a layer** that costs nothing in fidelity: the final gate (full suite, ~2 min) and the
  review (Muse, ~1 min) read the same committed tree and do not depend on each other; the pre-code
  check's suite on the tester's tree and `_failing_at`'s run on the base tree are independent subprocesses.
  ~1–1.5 min a layer.

### 3. Three history edits defeat the prefix cache

**Evidence and cause.**

- **Carried reasoning removes a message mid-history.** `native_team.py:519` drops the previous
  `reasoning-carried` message before appending the new one. The stored key is the whole previous prompt;
  the new prompt diverges at the removed message, so the next step is a MISS. In c85e the step after each
  carry shows `prefill 20.5 / 23.9 / 27.0 / 35.6 s` against ~2 s elsewhere; a146 shows two in a row
  (`28.0, 28.2 s`; `server.log`: `MISS prompt_tokens=24769`, `MISS prompt_tokens=24952`). ~25 s per
  carry, 14 carries in the window. **Fix:** append the new carried message and leave the old one (1,500
  chars each); appending after the tool result keeps every stored prefix valid.
- **Fresh continuation.** 22 events, every one at 33–70k estimated tokens (`CHARS_PER_TOKEN = 4`
  underestimates code, which runs ~3): each is a full re-prefill of the brief and the role's own diff, and
  the role starts with no memory of what it did (AIR-9 layer 05: a fresh tester re-read its files for all
  20 steps). A one-time compaction instead, dropping old tool-observation bodies but keeping the role's
  calls and text, costs one re-prefill of ~30 s and keeps the working memory; the whole conversation
  shrinks by the observations (the bulk of it, reads are capped at 20k chars each).
- **Context trim** (`on_context_full: trim`) rewrites old tool messages in place: a MISS on the whole
  suffix every time it fires. It is off by default; if it is turned on, trim once in a block, not
  per-step.

### 4. Each layer pays ~7–8 minutes of fixed overhead, half of it the whole test suite

**Evidence.** From the two layers above: pre-code check 1.9–2.2 min, final gate 1.8–2.0 min, review
0.7–1.0 min, the tester's final reply 1.8–3.4 min. The suite is 2,739 tests in 166 s here with `-n auto`
(8 workers): 367 s user, **350 s system**, which says the time is process spawning, not Python. The pre-code
check runs it whole (`PytestChecks._suite`, `--continue-on-collection-errors -rA`) once per attempt (up to
2), plus `_failing_at` on a fresh base worktree when existing tests fail; the final gate runs it whole
again; `gate_follow_up` once more on a red gate. Three to five full suites a layer is 8–14 minutes.

**Changes.**

- **The suite itself.** `uv run pytest --durations=30` (run here, list at the end of this file): the 30
  slowest tests take 6–16 s each and every one is in `test_story_publish`, `test_story_pipeline`,
  `test_story_layer_checks_flow`, `test_story_restack` or `test_story_factory`, which build real git
  repositories and drive the real `git`/`gh` paths per test. Those are integration tests sitting in the
  unit layer; move them to `tests/integration/` (stage 3 of the pipeline, which the gate inside a story
  does not run), or share one prepared repository per module and `git worktree add` from it per test.
  The remaining 2,700 tests should run in well under a minute on 8 cores.
- **mypy in a fresh worktree is cold: 18.8 s** (0.7 s warm, measured here). Every layer's worktree starts
  without `.mypy_cache`, so the first `run_tests` and the first gate pay it. Seed the worktree's
  `.mypy_cache` from the checkout the story runs in (`shutil.copytree`, ~tens of MB) in
  `add_worktree`'s caller, or point `MYPY_CACHE_DIR` at a per-base-commit directory under the state dir.
- **The final no-tool reply.** A thinking role that decides it is done thinks for 2–3 minutes to say so.
  `last_turn_thinking: false` covers only the `max_steps` turn. A role whose last `run_tests` was green can
  be offered its closing turn without thinking (same trick, one more condition in `native_team.py:381`).
- **Pre-code check scope.** Its two questions are "do the new tests fail" (the changed modules alone,
  seconds) and "did the tester break an existing test" (the rest). Run the changed modules first and
  skip the rest when the first question already produces findings, since the retry re-runs everything.

### 5. Refused reads are paid in full

**Evidence.** 102 `read_refused` events in the window. The hold is enforced where the call runs, not in
the schema, so the prefix stays cached (`_offered`, AIR-28: one changed tool list re-prefilled 56,717
tokens once). But each refused call is a complete generation first: 35 s for a plain tester step, 260 s
for one that thinks to the cap. The trade is one ~30–60 s re-prefill against N refused generations; at the
observed rates the re-prefill wins after the first refusal. Offer write-only schemas when the hold starts,
accept the one miss, and keep `read-left` as the text.

### 6. Context growth taxes every later token

**Evidence.** `output_cap_chars: 20000` per observation (~5k tokens). The tester reads on 54 % of its
steps (255 reads, 62 greps in 471 steps); median prompt 26k, max 58k. Decode falls from 25.7 tok/s at 4k to
17 at 26k to ~11 at 50k, so a read early in a run makes every later step slower, and prefill of a fresh
start grows with it. `repo_guide` already hands the named files whole; `step_report.py` can say how many
reads were of files the brief already carried.

**Changes.** A default `read_file` window (say 200 lines unless `limit` is given; the tool already says
how to read on), an outline for files over a size, and `grep` returning the first 100 matches (not 500).
Each is a one-line cap. Expect a flatter prompt curve, faster decode on later steps, and smaller
snapshots in the prefix cache (finding 2).

### 7. Engine configuration

- **The running engine is the base profile, not the studio's.** `ps`: `--max-kv-size 65536`, reviewer
  not preloaded (loaded lazily at line 134155 of the log), no `--default-thinking-token-budget`. The studio
  profile declares `max_kv_size: 131072` and preloads the reviewer; the chief, architect and reviewer
  declare `max_context: 131072`. vllm-mlx reports `Max KV size: 65536 (RotatingKVCache)`: a prompt past
  64k rotates (loses its head) and the hybrid patch cannot store a rotated cache. One prompt in the window
  reached 72,628 tokens. `task restart -- --topology studio` before story work; `task doctor` could warn
  when the engine's flags do not match the story's profile.
- **`prefill_step_size: 2048`.** Prefill on the hybrid is ~900 tok/s. Larger steps (4,096, 8,192) are
  the usual MLX win on long prompts; `task bench -- --ctx 32k --n 1` before and after is a 10-minute
  check.
- **`--warm-prompts`** (vllm-mlx 0.5.0): pre-run each role's system prompt at start so the first step of
  each role hits. Small (the system prompts are 1–3k tokens); free.
- **`stream_interval 1`** is right for TTFT measurement; fine.

### 8. The doctor's KV model is 4× too high for the hybrid, and it is steering context choices

**Evidence.** `models.generalist.kv_mb_per_1k_tokens: 256`. The architecture (`config.json`): 64 layers,
`full_attention_interval 4` → 16 full-attention layers, 4 KV heads, head dim 256; K and V in bf16 =
64 KB/token = **64 MB per 1k tokens**; the 48 linear-attention layers keep a fixed-size state. The
`studio-coder8` note in `airlie.yaml` fell back to a 65,536 KV because "131072 KV projected 111 GB vs the
104 GB wired limit"; at 64 MB/1k the KV is 8.4 GB, not 33.5. The reviewer's entry (64) was derived the
right way. `project_memory_gb` also multiplies by `teams.k`, not by concurrent sequences, which is what
parallel stories will need.

### 9. `task serve` does blocking work on the event loop that also runs the story

- `metrics._rows` reads every `events.jsonl` under `runs/` and `stories/` (264 files, 4 MB here) on each
  `/v1/metrics/*` request, synchronously; `duckdb` is imported and unused. `recent_runs` parses 50 event
  files per `/v1/airlie/runs`. `Jobs.status` re-reads the whole events file every 250 ms per long-poller.
  Every `git` helper in `teams/worktree.py`, `tools/scope.py` and `repo_map.py` is a synchronous
  `subprocess.run`. In the serve process these pause the running story's loop and the UI alike. Wrap the
  scans in `asyncio.to_thread` and cache by file mtime, or let DuckDB do `read_json_auto` over the glob;
  move the git helpers behind `asyncio.to_thread` at the few call sites in the pipeline (commit, diff,
  fingerprint).

### 10. The repo-map cache never hits

**Evidence.** 67 `repo_map` events, `cached: false` in every one. The key hashes each tracked file's
`mtime_ns`; every story layer runs in a fresh worktree, where every file has a new mtime. Key on the
commit's tree instead (`git rev-parse HEAD^{tree}` plus `git status --porcelain` for a dirty tree). Small
(a map is ~1–2 s), but it is a cache that has cost disk and never paid.

### 11. Leftovers on disk

`runs/` is 9.7 GB; six abandoned studio worktrees hold a 1.4 GB `.venv` each (`uv sync --frozen` in the
gate; hardlinked from uv's cache, so cheap to make, but `remove_worktree` is skipped when a layer blocks,
by design, and nothing prunes later). `observability.retain_days: 14` is set and runs from 2026-10-12
remain. A `task runs:prune` honouring `retain_days`, and `git worktree prune` with it.

### 12. Observability: the numbers that misled this review

- A reply with no tool call is not a `step` event (`native_team.py` records a step per call), so the
  final turn of every role, 2–3 minutes for a thinking one, is invisible to the metrics tab and to the
  per-step totals above.
- `prefill_ms` is TTFT. vllm-mlx with server-side tool parsing emits a parsed call as one delta at the end,
  so for a non-thinking tool-call reply TTFT includes the whole decode: the coder's "108 tok/s" and
  "6 s prefill" in the role table are one number split wrong. The metrics tab's `prefill_tps` for the coder
  is therefore wrong too. Use the engine's `usage` and the stream's first content/reasoning delta only, or
  flag single-delta replies.
- `cached_tokens` is always None: the hybrid patch logs hits at INFO in `server.log` but vllm-mlx reports
  0 in `usage`. Surfacing the patch's `cached=` in the response (or scraping the log line by request id
  into the step event) would make finding 3 visible in the trace instead of in a grep.

### 13. The supervisor's bill

15 triages in the ledger: 23.8 min, 73–158 s each, 60k–132k tokens each (a tool loop of up to 6 Muse calls,
each re-sending the brief, 20k of diff, the trace tail and the events). Advisory and a trial, so a cost
Noah chose; `spec.trace_steps` and `DIFF_CAP` are the knobs, and the triage could skip the tools loop for
`block` (the layer is stopping; nothing it finds changes the outcome).

### 14. Dev loop and CI

The gate is `uv sync`, ruff ×2, mypy (18.8 s cold), pytest (166 s). CI runs it on `macos-14` runners
(3–4 cores), then five mutation shards with a 90-minute budget. The suite's system-time share (finding 4)
is the lever for both: the local gate, CI's unit stage, and every mutant's test run.

## Before parallel stories: why layers block (added the same evening)

Noah's question after reading this: he wants finding 2, but no story has gone through unattended. The story
logs (15 story directories, 57 `blocked` events, 68 block-or-cancel → resume gaps):

| what blocked | blocks |
| --- | --- |
| the final gate fails after the build | 27 |
| the tester's tests fail the pre-code check after its one retry | 14 |
| the layer changed nothing | 9 |
| product or architect did not finish (`max_steps`) or gave no usable plan | 5 |
| GitHub, or a request product refused | 2 |

One story went through clean (AIR-16: 3 layers, 92 min). The published ones took 4 (AIR-31), 5 (AIR-5),
9 (AIR-9) and 13 (AIR-7) blocks each. A block is resumed after a median 6 minutes when Noah is at the
keyboard; 14 of the 68 waits were over an hour (15 h and 65 h overnight). Every resume rebuilds the layer
from scratch (`_clear_attempt`), so a block costs the layer's 30–70 minutes plus the wait.

**The two most recent gate blocks are one bug** (AIR-17 layer 02, runs `20261020-210203-3cdf` and
`20261023-145448-5f10`, 2.5 hours between them). The trace: the coder reads, edits `board.js`, runs its
scoped tests (red), edits, runs them again (red), greps to see why, and from step 18 every call answers
"reading is switched off". It keeps grepping through `shell` (12 identical refusals), the repeat guard
ends it as `repeated_action`, `ok=False`, so `_gate` skips the gate retry ("only a subtask that
finished is sent back"), the gate fails on the half-made change, and the story blocks with "`uv sync …`
fails on …". The hold came from the coder's `write_schedule` (`tests: 0.25`): in a tester layer the coder
must not write tests, yet `write_deadline tests met: false` fired at step 10 in both runs. Commit `b33d4f2`
(2026-10-19) meant the tester's files to count as already written (`_context`: `already_written =
changed_in_tree(worktree, run.before)`), but `before_code` commits those files and the build subtask takes
`run.before = head_sha(worktree)` after that commit, so `already_written` is empty in every story layer and
the 2026-10-19 fix never fires there. Fixed on this branch: `_context` counts what changed since the run's
`base_sha`, committed or not, with a test for the committed case beside the one for the uncommitted case.
Still open: a build that ended `repeated_action` with a changed tree gets no gate retry today; whether it
should is a separate call.

**What this says about finding 2.** Parallel stories multiply the rate of blocks waiting for a person. The
same engine headroom spent on redundancy inside one layer raises the completion rate instead: N testers
(or coders) on the same layer at once, the pre-code check or the scoped tests as the oracle, the first to
pass continues and the siblings are cancelled (`teams.stop_on_first_pass` at layer granularity, with a hard
oracle this time; the 2026-10-13 fork bench failed on a soft one). At N=3 aggregate decode is ~3× single
stream, so a layer costs ~1.1–1.3× one attempt's wall-clock and gets pass@3 on the stage that blocks most.
It needs the same engine prerequisites as parallel stories (chunked prefill, explicit prefix-cache memory,
the doctor's per-sequence model) and is the groundwork for them. Cheaper still: let the tick resume a
blocked story up to a budget before it waits for a person, so an overnight block costs a rebuild, not a
night.

## Suggested order

Each is a PR-sized change; the first four need no decision beyond "yes":

1. Carried reasoning: append, never remove (finding 3). One line; ~25 s back per carry.
2. Seed `.mypy_cache` in the layer worktree; profile and fix the slowest tests (finding 4).
3. Fix `kv_mb_per_1k_tokens` for the generalist and the doctor's per-sequence projection (finding 8); then
   set the studio profile's KV honestly and make `task doctor` compare the running engine's flags with the
   story profile (finding 7).
4. `--chunked-prefill-tokens`, `--cache-memory-mb`, `--kv-cache-quantization` in the studio profile's
   `extra_args` (finding 2): the prerequisites for parallel stories, harmless today.
5. MTP weights for the generalist and one layer with `--enable-mtp` (finding 1): the biggest single win if
   acceptance is decent; a day including the add-weights step.
6. Tester thinking budget 2,048 / off: one layer each, judged by wall-clock to a built layer (finding 1).
7. Write-only schemas under a hold (finding 5); `read_file` default window (finding 6); the green
   closing turn without thinking (finding 4).
8. Compaction in place of fresh continuation (finding 3).
9. Serve-process offloading and the metrics scan (finding 9); repo-map key (10); pruning (11);
   step/TTFT observability (12).

## Method

Step events: `kind == "step"` in `runs/*/events.jsonl` since 2026-10-19 (prefill_ms, gen_ms, tool_ms,
prompt_tokens, completion_tokens, role, model). Layer timelines: `stories/*/events.jsonl` joined to the
studio runs they name, with gaps between consecutive events attributed to the pipeline work between them.
Cache: the `[mllm_cache]` lines of `engines/main/server.log`. Suite, mypy, ruff and pytest timings:
measured in this worktree on 2026-10-23 with the engine idle. Model geometry: the quant's `config.json`.
vllm-mlx capabilities: the installed 0.5.0 (`cli.py`, `memory_cache.py`, `patches/qwen3_5_mtp.py`,
`mllm_batch_generator.py`).

## Appendix: the 30 slowest unit tests (2026-10-23, `-n auto`, 8 workers, 2,739 passed in 143 s)

| s | test |
| --- | --- |
| 15.9 | test_story_publish::test_a_publish_step_resolves_github_without_a_list_a_model_planted[you] |
| 15.8 | test_story_publish::test_a_publish_step_resolves_github_without_a_list_a_model_planted[the bot] |
| 14.1 | test_story_layer_checks_flow::test_a_tester_that_stands_by_its_tests_changes_nothing_and_the_coder_is_sent_the_gate |
| 12.6 | test_story_pipeline::test_a_failed_layer_keeps_its_worktree_and_branch_and_the_note_says_what_the_next_run_does |
| 10.7 | test_story_layer_checks_flow::test_a_mend_that_passes_before_the_code_is_undone_and_the_coder_is_sent_the_gate |
| 10.5 | test_studio_orchestrator::test_chief_router_without_a_usable_hint_falls_back_instead_of_raising |
| 10.1 | test_story_layer_checks_flow::test_a_mend_that_changes_the_code_is_undone |
| 9.9 | test_story_pipeline::test_a_resume_builds_on_the_base_the_story_started_from_not_on_a_newer_one |
| 9.5 | test_story_restack::test_built_layers_move_onto_the_trunk_and_a_resume_builds_on_them |
| 9.4 | test_story_pipeline::test_a_resume_builds_with_the_gate_the_story_read_when_it_started |
| 9.3 | test_story_pipeline::test_the_test_command_is_masked_wherever_it_is_shown |
| 9.3 | test_story_layer_checks_flow::test_a_final_gate_still_failing_after_the_follow_up_stops_the_layer |
| 8.9 | test_story_pipeline::test_a_failing_layer_stops_and_a_resume_rebuilds_only_that_layer |
| 8.8 | test_story_pipeline::test_each_pr_body_names_the_gate_its_own_layer_passed |
| 8.6 | test_story_pipeline::test_a_tag_named_like_a_layers_branch_is_not_the_next_layers_base |
| 8.2 | test_story_publish::test_as_the_bot_a_step_pins_tls_and_addresses_for_each_url_it_uses |
| 8.1 | test_story_restack::test_a_layer_whose_gate_fails_on_the_new_trunk_changes_nothing |
| 8.0–8.1 | test_story_publish::test_a_publish_step_uses_no_proxy_a_model_planted (6 parametrisations) |
| 7.9 | test_story_restack::test_a_layer_that_conflicts_with_the_trunk_changes_nothing |
| 7.5 | test_story_restack::test_a_story_already_on_the_trunk_is_left_alone |
| 7.5 | test_story_restack::test_a_published_story_is_not_restacked |
| 7.3 | test_story_factory::test_an_issue_put_back_in_to_do_after_its_story_finished_goes_to_review_and_the_next_is_built |
| 6.5 | test_story_pipeline::test_a_gate_edited_in_the_persons_checkout_or_in_a_layers_worktree_is_not_the_gate |
| 6.5 | test_story_layer_checks_flow::test_without_a_tester_an_assertion_free_test_twice_stops_the_layer |
| 6.3 | test_story_pipeline::test_a_layer_that_changes_nothing_stops_the_story |
