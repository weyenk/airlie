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
