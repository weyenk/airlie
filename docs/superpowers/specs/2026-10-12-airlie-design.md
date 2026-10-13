# airlie design — architecture over the build spec

**Source of truth:** `docs/wiki/build-spec/01–04`. This document records how airlie realises that spec, which
seams are configurable, and the handful of places where the spec is internally inconsistent or asks for something
the chosen engine cannot do. When this document and the spec disagree, the spec wins and this document is wrong.

Status: approved by Noah 2026-10-12 (rev 3.1 of the roadmap plan, kept at `~/.claude/plans/`, is the living copy;
this file is the committed snapshot).

## What airlie is

A Python 3.12 / asyncio / FastAPI / Pydantic v2 orchestrator (spec 01 §2) that runs K parallel agent teams on one
Mac Studio, shares what they learn through a Learning-to-Share memory bank with an admission controller (spec 03),
schedules them EdgeAgent-style (spec 04), and sends every generation request over HTTP/SSE to a vllm-mlx server on
`127.0.0.1:8080` that airlie starts and stops (spec 02). The first workload is coding tasks in a git repository,
graded by hidden tests.

## Topology

```
 task run / task serve / any OpenAI-compatible client
            │
            ▼
 ┌──────────────────────── airlie orchestrator (Python 3.12 / asyncio / FastAPI / Pydantic v2) ──────────────┐
 │  ParallelAgentTeamOrchestrator (spec 04 §3)                                                               │
 │    TeamOrchestrator ×K  [AutoGen default | native]  each: ReAct + tools + isolated git worktree           │
 │        │ (u_t, s_t, o_t)                                                   ▲ [Shared Memory Keys] / fetch │
 │        ▼                                                                   │                              │
 │  LTS: Summarizer → LTSAdmissionController C_θ → SharedMemoryBank                                          │
 │  EdgeAgent scheduling: HALSpeculativeScheduler (L_total=16) + slots; ACTIVE ⇄ SUSPENDED_TOOL_STALL         │
 │  Engine seam ── HTTPEngine ──► managed vllm-mlx process (task up; --models-config) ──► or any OpenAI URL  │
 │  LLM Aggregator · Events/Trace · Doctor · Bench · Process manager                                         │
 └───────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                      │ HTTP / SSE, 127.0.0.1:8080
                                      ▼
 ┌──────────────────────────────── vllm-mlx serve (MLX 0.32+) ───────────────────────────────────────────────┐
 │  continuous batching · prefix KV cache · scheduler · MTP · multi-model residency                           │
 └───────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Seams

Every seam is selected in `airlie.yaml`, has a protocol, and has a fake for tests.

| Seam | Protocol | Implementations | Config key |
| --- | --- | --- | --- |
| Inference | `Engine` | `HTTPEngine` (managed `vllm_mlx` process or external `openai_http`); `FakeEngine` | `engines.<name>.kind` |
| Model catalog | `ModelSpec` | HF repo + pinned revision or local path; `bench`/`tags` for comparisons | `models.<name>` |
| Roles | `RoleSpec` | prompt + model + sampling + thinking + caps; engine derived from model | `roles.<name>` |
| Team orchestrator | `TeamOrchestrator` | `autogen` (default, per spec), `native` | `teams.orchestrator` |
| Tools | `Tool` | builtin fs/shell/git/tests, `retrieve_value`, MCP client | `tools.*`, `mcp.*` |
| Admission | `LTSAdmissionController` | `always` (spec blueprint), `never` (baseline), `heuristic`, `judge`, `learned` (spec 03 §2 torch model) | `lts.controller.kind` |
| Summaries | `Summarizer` | `truncate` (spec blueprint `output[:100]`), `role` (LLM, 15–20 words) | `lts.summarizer.kind` |
| Memory bank | `SharedMemoryBank` | in-memory + JSONL per run | `lts.*` |
| Scheduling | `Scheduler` | `HALSpeculativeScheduler` + slot accounting | `scheduler.*` |
| Aggregation | `Aggregator` | `llm` (spec 01 §4 step 9), `test_score` | `aggregator.kind` |
| Observability | `EventSink` | JSONL events + per-run trace | `observability` |

Hard rules: models are reached only through the `Engine` seam, except the LTS controller C_θ which is the spec's
in-process torch module; every long-running thing is cancellable; every tool call is timeboxed; every run writes a
trace; config errors name the key path; no client-specific hooks.

## Decisions and deviations

| Topic | Spec says | airlie does | Why |
| --- | --- | --- | --- |
| Base model | Llama-3.3-70B-Instruct-4bit or Qwen2.5-32B-Instruct-4bit | default `Qwen3-Coder-30B-A3B-Instruct-4bit`; both spec models and Qwen3.8-27B in the catalog, pinned, `bench: true` | Noah: modern model for the same slot is fine if configurable and benchmarkable. Qwen3.8-27B was the first default but is hybrid-attention (see below) |
| Framework | AutoGen / LangGraph | AutoGen default, native fallback behind one seam | Noah: stick with the spec |
| `--max-kv-size` | 8192 (02 §1) vs 32K–64K per team (01 §1) | 65536, one-line override | internal inconsistency; coding agents need the context |
| Controller base | text: Qwen3-0.6B; code: Qwen2.5-0.6B-Instruct; YES/NO ids hard-coded | Qwen3-0.6B; ids looked up from the tokenizer | internal inconsistency |
| Per-request draft budgets (HAL) | scheduler sets L_i per agent | HAL computes budgets; applies them only when the engine reports `per_request_draft` | vllm-mlx 0.5.0 sets MTP draft length per server, not per request |
| CPU/GPU heterogeneous TP, SME2 kernels | spec 02 §2 | not done | engine-internal; vllm-mlx has no such mode |
| Benchmark | AssistantBench / GAIA | coding tasks first (`task bench:lts`), GAIA runner later (`task bench:gaia`) | Noah's first workload |
| Project name | `m5-mlx-agent-framework` | package `airlie` | repo name |

## Memory budget (spec 01 §1)

`task doctor` projects: Σ `estimated_memory_gb` of preloaded models + `teams.k` × `max_kv_size` × `kv_mb_per_1k_tokens`
+ 12 GB OS/orchestrator reserve, and fails if the sum exceeds `iogpu.wired_limit_mb`. Defaults put the Qwen3.8 stack
at roughly 16 + 48 + 3 + 12 ≈ 79 GB and the Llama-70B stack at ≈ 100 GB, both under 104 GB, matching the spec's
83–98 GB envelope.

## vllm-mlx 0.5.0 wire format (recorded 2026-10-12, `tests/fixtures/vllm_mlx/`)

Recorded with `scripts/record_fixtures.py` against `vllm-mlx serve --continuous-batching --reasoning-parser qwen3
--enable-auto-tool-choice --tool-call-parser qwen` and the `small` model. HTTPEngine is written against these.

- **Streaming** is standard OpenAI `chat.completion.chunk` SSE ending with `data: [DONE]`. The last content frame
  carries `finish_reason`; one more frame with `choices: []` carries `usage` (`prompt_tokens`, `completion_tokens`,
  `total_tokens`). A `{"delta": {"role": "assistant"}}` frame can appear more than once mid-stream; it is ignored.
- **Reasoning** arrives as `delta.reasoning_content` frames (Qwen3 parser), then `delta.content`.
- **Tool calls** arrive as whole objects in `delta.tool_calls[i]` (`id`, `type`, `function.name`,
  `function.arguments` as a JSON string), **emitted twice**: once with `finish_reason: null` and again on the
  `finish_reason: "tool_calls"` frame with a different `id`. HTTPEngine merges by `index`, so one call results.
- **No `cached_tokens`.** `usage` has no `prompt_tokens_details`; prefix-cache hits are measured by TTFT (and
  `/metrics` when enabled), not reported per request.
- **`logprobs` is ignored**: the response has no `logprobs` field. `yes_no` therefore uses the constrained one-word
  text answer; `EngineCapabilities.logprobs = False`.
- **Errors** are FastAPI-style: `404 {"detail": "The model `x` does not exist. Available model: `small`"}`; OpenAI
  `{"error": …}` is also handled.
- **`GET /health`** → `{"status": "healthy", "model_loaded": true, "model_name": …, "available_models": [...],
  "engine_type": "batched"}`; **`GET /v1/models`** → `{"data": [{"id": "small", …}]}`.
- **Registry mode (`--models-config`)**: `/health` reports `available_models` = every registered name and
  `model_name: null`; it answers only after all `preload: true` models have loaded (server.log shows both loads before
  "Application startup complete"). Non-preloaded models load on first request, so `task status` shows them as `lazy`.
- **Qwen3.8-27B-4bit loads as an MLLM** (`mllm=True`, "Native video pipeline enabled"), so vllm-mlx uses its MLLM
  scheduler for the base model; MLLM-specific limits (MTP only at batch size 1, fixed MLLM prefill step) apply.
  First request after startup: 1.1 s for 16 tokens.

## Prefix cache and hybrid-attention models (measured 2026-10-12)

Spec 02 §3 requires prefix caching so an agent's next turn does not re-prefill the conversation. Measured through
`HTTPEngine` against the managed vllm-mlx engine, three turns sharing a 4k-token system prompt:

| Model | Attention | Turn 1 TTFT | Turn 2 TTFT | Turn 3 TTFT | Server log |
| --- | --- | --- | --- | --- | --- |
| Qwen3-4B-4bit (`small`) | standard | 975 ms | 72 ms | 81 ms | `cache_fetch HIT cached=4017` |
| Qwen3.8-27B-4bit | hybrid (3 of 4 layers `linear_attention`) | 4455 ms | 4232 ms | 4248 ms | full chunked prefill every turn, no fetch |
| Qwen3.8-27B-4bit, identical prompt ×3 | hybrid | 3935 ms | 3777 ms | 3768 ms | store skipped |

Cause, from vllm-mlx 0.5.0 source: `memory_cache.py` blocks longest-common-prefix lookups for hybrid models
("SSM state can't be rewound") and `mllm_batch_generator._store_prefix_snapshot` skips the store when the cache
"cannot be rewound safely". So Qwen3.5/3.8-class models (Gated DeltaNet + attention) get no prefix-cache benefit at
all in this engine version, and a 20k-token conversation costs ~21 s of prefill on every single step.

Decision: team roles use a standard-attention base model (`Qwen3-Coder-30B-A3B-Instruct-4bit`, MoE, standard GQA).
Qwen3.8-27B stays in the catalog (`qwen38`, `bench: true`) for `task bench:models`, and `task doctor` warns on any
hybrid-attention catalog model. Revisit when vllm-mlx can snapshot hybrid caches.

## M1 result (2026-10-12)

`task up` starts one vllm-mlx process from `airlie.yaml` (registry: base + small preloaded, qwen38 / llama70b /
qwen25_32b lazy), `task status` shows per-model readiness, `task test:live` passes against it, and `task bench`
measured the default team model at 112 tok/s decode / 4.2k tok/s prefill (4k, N=1) with a turn-2 prefix-cache TTFT
ratio of 0.1 (full table: `docs/bench/2026-10-12-engine-bench.md`). Spec 02 §1 launch parameters and §3 prefix
caching are therefore in effect for the team model.

## M2 result (2026-10-12)

`task eval -- lang_detect --k 1` runs end to end against the managed engine: an AutoGen team (spec 01 §2) in its
own git worktree, sandboxed tools, slot accounting with suspend-and-yield on every tool call (11 yields recorded),
every step traced. Hidden tests: 9/9 pass in 11 steps, 87 s wall-clock, on the default base model.

Two engine quirks handled at the client boundary (`teams/autogen_client.py`, `tools/jsonargs.py`): vllm-mlx emits
each streamed tool call twice, and its `qwen3_coder` parser can append a stray `}` to JSON arguments when a file
body contains braces. Both are repaired before AutoGen parses them; the native loop uses the same repair.
Run-to-run variance is real: of four live runs, two finished cleanly (24 s / 87 s), one hit a requirement
ambiguity (now fixed in the task), one looped on shell experiments until `max_steps` (prompt tightened, coder
temperature 0.3).

## M3 result (2026-10-12)

The Learning-to-Share substrate is live: `SharedMemoryBank`/`MemoryEntry` (spec 03 §1 verbatim), summary keys
written by the `summarizer` role on the small model, admission by the configured controller (`heuristic` default;
`always`, `never`, `judge` selectable), keys injected before every model call in the spec 04 §3 format, and a
`retrieve_value(entry_id)` tool per team. Every decision lands in `runs/<id>/lts/admission.jsonl` with
`used_later` and `episode_reward` filled after the run, plus `retrieval.jsonl` and `memory.jsonl` (`task memory`).

Live `task eval -- lang_detect --k 3`: 3 teams in parallel (max_concurrent 3, 48 yields), 29 entries admitted of
48 steps, 2 of 3 worktrees pass the hidden tests, 246 s wall-clock. **No cross-team retrievals happened**: the keys
were injected (visible in the traces) but the agents never called `retrieve_value`. With three teams on an
identical small task there is little to fetch; the retrieval path is unit-tested and the behaviour on real,
divergent work is what M4's `task bench:lts` measures. Two loop fixes came out of this run: the token budget now
counts context + generated tokens (resent history is prefix-cached and was exhausting the budget), and the
repeated-action stop requires identical outputs (a run-tests fix loop is not a stuck loop).

## LTS follow-up (2026-10-12): admission off the critical path, ranked injection

The first `task bench:lts` on the bigger tasks (`docs/bench/2026-10-12-lts-bench-bigger.md`) showed LTS as pure
overhead: `after_step` awaited the summarizer and the controller before the team's next turn, every step
re-injected the last 32 keys, and no team ever called `retrieve_value`. Two changes, both behind config:

- **Background admission** (`lts.background_admission: true`, `lts.admission_concurrency: 2`). `after_step`
  schedules summarize + decide as a task under a semaphore and returns at once; the orchestrator calls
  `LTSObserver.drain()` after the teams finish, before aggregation, so every decision is in the bank and the log
  before the run is reported. A failing summarizer or controller emits `admission_error` instead of taking the
  team down. Spec 01 §4 step 5 still holds: admission happens after step completion; it just no longer blocks
  the next step. The blueprint `run_step` keeps the synchronous order.
- **Ranked injection** (`lts.ranking: relevance | recency`, `lts.max_keys: 12`, `lts.inject_own: false`,
  `lts.inline_top: 2`, `lts.inline_chars: 1500`). Keys are ranked for each team by distinct-token overlap between
  the key (plus the head of its value) and the team's context (task query + its last step), identifiers split
  into parts; ties stay newest first; a team never sees its own keys; the top fresh keys carry their value inline
  once per team, so the obvious hits need no tool call. The key lines keep the spec 04 §3 `- Key [id]: summary`
  format and `recency` restores the blueprint order. A spec deviation, stated: spec 04 §3 injects *every* key;
  the ranked, own-excluded, value-inlined block is what makes spec 01 §4 step 7 ("if relevant, they fetch o_i")
  reachable for a coding agent that otherwise never fetches.

## Team loop default (2026-10-13): native, AutoGen selectable

Noah's call after the fork-when-stuck smoke runs. AutoGen's `AssistantAgent` is used only as the per-team ReAct
loop; airlie uses none of its multi-agent features. Three of its behaviours needed shims on the same day (tool
failures are not `is_error` unless the tool raises; its OpenAI client has no `top_k`/`min_p`/`repetition_penalty`
fields; argument decoding errors are pydantic dumps), while the built-in loop implements the same `TeamOrchestrator`
contract in ~130 lines with direct control of sampling, state and errors. `teams.orchestrator` now defaults to
`native`; `autogen` remains one config line away for spec 01 §2 conformance and is covered by the same tests.
Both loops carry the same guards (one tool call per step, named missing arguments, cut-off arguments reported,
per-request repetition penalty).

## Topology result (2026-10-13)

`task bench:topology` on lang_detect, size_parse, inventory_cli × 3 repeats (`docs/bench/2026-10-13-topology-bench.md`):
single agent 5/9 at 39 s mean on solved runs; the spec's K=3 + LTS 5/9 at 57 s; fork-when-stuck 2/9. Neither
candidate met the design §9 criterion against the single agent, so the default is now the single agent
(`teams.k: 1`, `lts.enabled: false`); the spec's topology and `fork` stay one config line away. Fork's losses are
mechanical and listed in the write-up (trigger at the second failing test run is too early; best-effort resolve
accepts a child's "done" without a passing test; 15 child steps too few) and worth one tuning pass before
drawing a final conclusion on the idea. Audit-table status for spec 01 §2 / 04 §3 topology: 👤 single agent by
evidence; K=3 + LTS selectable.

## M5 result (2026-10-12)

The spec 03 §2 controller is implemented as written (frozen Qwen3-0.6B embedder φ, `proj_query/proj_mem/proj_step`,
LoRA r=16 α=16 through peft, YES/NO logits at the last position; token ids resolved from the tokenizer) and trained
with the spec 03 §3 objective over the logged decisions of every run. Live: `task controller:dataset` found 92
decisions in 3 runs (44 train / 48 holdout by run), `task controller:train -- --epochs 3` on MPS took 29 s with
loss 0.279 → 0.132, and `task controller:eval` replays decisions. **The usage signal is empty**: no cross-team
retrieval has happened in any run, so `used_later` is false everywhere, usage-weighted precision/recall are 0 for
both policies, and the trained controller is at this point only learning the outcome reward. The pipeline is
complete; it becomes meaningful once runs produce retrievals (larger, divergent tasks; M4b). Selecting it is one
config line: `lts.controller: {kind: learned, adapter_path: ~/.local/state/airlie/controller/latest}`.

## M6 result (2026-10-12): speculative draft budgets

Spec 04 §1 wants the HAL scheduler to set each agent's speculative draft length L_i. Checked against vllm-mlx
0.5.0 source: MTP is installed once per `BatchGenerator` (`scheduler._install_mtp`, a monkey-patch), the draft
length is the server-level `mtp_num_draft_tokens`, and the batched path "drafts exactly one token per verify
step" (warning at `scheduler.py:1281`). There is no per-request draft-length field in the chat API. So budgets
cannot be *applied* with this engine; the 🚫 row in the audit stands.

What is wired: the engine reports `generation_metadata.mtp_accepted` / `mtp_drafts` per response, `HTTPEngine`
surfaces them as `GenResult.accepted_draft_tokens` / `draft_tokens`, every step carries them, and the
orchestrator's `HALObserver` runs `update_hal` and `allocate_budgets` after each step (events `hal`). The budget
reaches the request as `GenRequest.draft_budget`, sent as `num_draft_tokens` only when
`engines.<name>.per_request_draft: true` (`EngineCapabilities.per_request_draft`). Flipping that flag is the whole
change once an engine supports it; the upstream patch would add a per-request draft length to
`_install_mtp`'s step (and to `GenerationRequest`), which is where the `requested_draft_tokens` stat already lives.

Not measured: `--enable-mtp` throughput. The default base (Qwen3-Coder-30B-A3B) has no MTP head, the MTP checkpoint
is Qwen3.8-27B (hybrid attention, no prefix cache), and comparative benchmarking is not a deliverable (Noah,
2026-10-12); `task bench -- --model <mtp model>` with `extra_args: [--enable-mtp]` runs it whenever wanted.
