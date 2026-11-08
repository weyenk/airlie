# mlx-vlm engine design

Approved by Noah 2026-11-08. Readable copy: https://claude.ai/artifact/FiozjvAvYsRm7YnuQcZA9Q

Nov 8, 2026 · @Ashley

## Intent

Replace vllm-mlx and llama-server with mlx-vlm's own server as airlie's only local engine, so airlie carries no engine patches and sits one layer above Apple's MLX instead of two. The trigger was the vllm-mlx 0.6.0 release: it caps mlx-lm below 0.32, airlie needs mlx-lm 0.32.0 for the Muse Glimmer reviewer, and upstream main had not lifted the cap on 2026-11-07. The deeper reason is Noah's: "if I can remove tools and still get the same quality, that sounds like a big win."

mlx-vlm is the vision runtime vllm-mlx already builds on. Its server (release 0.7.12, 2026-11-04, [Neywa-Labs/mlx-vlm](https://github.com/Neywa-Labs/mlx-vlm)) covers every feature airlie takes from vllm-mlx today and does not depend on mlx-lm at all, so the version conflict disappears with the layer. Within Apple's own repositories nothing fills this role: mlx-lm's server is text only, with no thinking budget and no MTP for Qwen, and mlx-swift-lm is a Swift library with no server and no batching.

This document is the design and the phased plan. Noah approves it, then a less expensive model implements it phase by phase. Nothing has been built or spiked; every claim about mlx-vlm below comes from reading its 0.7.12 source and docs, and the validation phase exists to test them on real runs.

## Goals, non-goals, anti-goals

Goals:

- Zero airlie code inside the engine process. No monkey-patches, no vendored parsers, no source-text rewrites.
- One fewer layer: airlie, then mlx-vlm, then mlx. mlx-lm, vllm-mlx and llama-server leave the dependency tree.
- Same quality as today, measured before the old engine is deleted. Same output, tokens per second and time to first token not worse, measured by the gate's own tests.
- Every feature airlie uses today keeps working: batching, hybrid-cache prefix reuse, MTP speculation, per-request thinking budgets, tool-call parsing for the coder and reviewer models, pictures and video in chat, KV quantization, engine health and busy probes. Today's workflows do not complete, so they cannot be the bar. Each feature is tested on its own, on both engines, to show what the swap gains or loses (Feature comparison, under Validation gate).
- As few tools as possible without losing quality, so llama-server for Kev goes too. A Jev-type decision model served by mlx-vlm takes its place, chosen by the comparison in the last section, so the engine side is one server.

Non-goals:

- Changing the pipeline, the roles, the prompts or the workflows. The engine is swapped under the Engine seam and nothing above it should notice.
- Chasing mlx-vlm main. Airlie pins one released version and moves deliberately, starting from the latest release that passes phase 1 (0.7.12 when this was written); the pin is re-checked against PyPI when phase 1 starts.
- Addressing Noah's wider middleware and backend concerns. That is its own conversation after this lands.

Anti-goals, things that would make this a failure even if it technically works:

- Carrying a patch into mlx-vlm "just for now". If mlx-vlm needs a fix, it is filed and fixed upstream or the feature waits.
- Deleting the vllm-mlx engine before the validation gate passes. A regression discovered after the delete is a rollback, not a fix-forward.
- A quiet quality drop hidden by a faster number. Speed counts only beside the feature tests' fidelity and quality results, never on its own.

## Where we are

Airlie runs vllm-mlx 0.5.0 on mlx-lm 0.32.0 and mlx-vlm 0.7.6, a pairing vllm-mlx never tested, and reaches into vllm-mlx in six places. All six are applied at engine start by airlie's own entry point, `src/airlie/engine/vllm_mlx_serve.py`, which wraps vllm-mlx's CLI.

| Airlie piece | What it does | How it depends on vllm-mlx internals |
| --- | --- | --- |
| `engine/patches/hybrid_prefix_cache.py` (PR 103) | stores the prefix cache at the last turn boundary so Qwen3.8's recurrent cache and Muse's rotating cache get hits | regex-rewrites the source text of two private methods and recompiles them |
| `engine/patches/mtp_batch_join.py` (PR 181) | settles decoding rows before a newcomer joins the batch so MTP does not corrupt concurrent streams | reads closure variables and private batch-generator and logits-processor fields |
| `engine/patches/active_requests.py` | adds in-flight counts to `/v1/status` for the busy probe | wraps the model registry after a source-text check |
| `engine/patches/first_output.py` | stamps time-to-first-token into the usage chunk | replaces the server's streaming function |
| `engine/parsers/qwen3_xml_raw.py` | parses Qwen3-Coder XML tool calls with raw values (four upstream parser defects, bench 2026-10-14) | subclasses the tool-parser base class and registers by name |
| `engine/parsers/muse_glimmer.py` | reasoning and tool-call parsers for Muse Glimmer, from unmerged upstream PR 812 | subclasses the reasoning and tool-parser base classes |

Beside these, `engine/mtp.py` builds a model directory in the on-disk layout vllm-mlx's Qwen3.5 MTP patch expects, and `engine/output_parse.py` imports vllm-mlx's parser registries into airlie's own process for models whose output the server leaves raw.

The version wall: vllm-mlx 0.6.0 (2026-10-20) pins `mlx-lm>=0.31.3,<0.32.0` because mlx-lm 0.32 changed cache and batching APIs its scheduler monkey-patches ([PR 826](https://github.com/waybarrios/vllm-mlx/pull/826)). mlx-lm 0.31.3 does not ship the `muse_glimmer` architecture; 0.32.0 does. So airlie cannot take 0.6.0 without overriding the cap, and upstream's own [issue 736](https://github.com/waybarrios/vllm-mlx/issues/736) says the scheduler's patch layer targets an mlx-lm API that no longer exists.

Work landing before this task: the nodes-are-config build (branch `nodes-are-config`, design `docs/superpowers/specs/2026-11-08-nodes-are-config-design.md`, plan 1 the flow engine) merges first. It rewrites `src/airlie/flow/*`, `src/airlie/story/flow.py` and the workflows API, and edits `src/airlie/config/schema.py` in the `AirlieConfig` area only. It does not touch `src/airlie/engine/*`, `EngineSpec`, `ModelSpec`, `http_engine.py` or the engine configuration. Its runners call the engine through the `Engine` protocol in `engine/types.py`, which this design keeps unchanged. The one shared file is `schema.py`; this work edits `EngineSpec` and `ModelSpec` and rebases onto main after that merge.

## Target architecture

Two engine kinds behind the one `Engine` protocol: `mlx_vlm` for every local model, the decision model that replaces Kev included, and `openai_http` for anything remote. The `vllm_mlx` and `llama_server` kinds are deleted after the validation gate.

One mlx-vlm process serves one generative model. mlx-vlm's server keeps one resident text model per process and unloads it when a request names another, so the registry-of-six that vllm-mlx held becomes a process per preloaded model. Every role resolves its engine through `engine_for_model`, as today, so the roles do not change.

Every seat is a rule, not a model, so no design choice leans on one model. The coder seat is the best coder available today (Qwen3.8-27B now). The reviewer seat is an adversarial agent from a different lab than the coder's (Muse Glimmer now). The small and decision seats work the same way. The engine kind does not care: swapping a seat means changing a model entry and a parser name, and the gate runs on whatever the entries name. Model-specific features such as an MTP draft head, a cache type or a tool parser are tested for whichever model holds the seat, not assumed.

Diagram in the Claude Doc: today airlie reaches MLX through vllm-mlx, which sits on mlx-lm and mlx-vlm and carries six airlie patches; in the target, four mlx-vlm processes (main, reviewer, small, decision) sit directly on MLX and nothing else serves models locally.

Today airlie reaches MLX through vllm-mlx, which sits on both mlx-lm and mlx-vlm and carries six airlie patches. In the target, four mlx-vlm processes sit directly on MLX, one of them the decision model, and nothing else serves models locally.

| Engine name | Kind | Model | Port | Started by |
| --- | --- | --- | --- | --- |
| main | mlx\_vlm | generalist: today's best coder (Qwen3.8-27B, 4-bit, MTP head) | 8080 | `task up` |
| reviewer | mlx\_vlm | reviewer: any adversarial model from a different lab than the generalist (today Muse Glimmer 30B, 4-bit) | 8082 | `task up` |
| small | mlx\_vlm | small (Qwen3-4B) | 8083 | `task up` |
| decision | mlx\_vlm | decision model (Clef-flash or decider-2b, whichever matches Kev) | 8081 | `task up` |

Memory stays within the budget vllm-mlx managed: the four preloaded models weigh about 16, 19 and 3 GB plus the decision model (2B or 9B parameters), plus their KV caches, and the doctor's projection already sums every managed process against the wired limit.

What moves where:

- Prefix caching moves from airlie's patch to mlx-vlm's automatic prefix cache, which stores checkpoints for windowed and recurrent layers at a common prefix boundary (the same rule as vLLM's hybrid cache).
- MTP moves from airlie's rebuilt directory plus batch-join guard to mlx-vlm's native MTP drafter: the head repo `mlx-community/Qwen3.8-27B-MTP-bf16` declares `model_type: qwen3_5_mtp`, which mlx-vlm's drafter registry maps to its MTP round loop, so it is passed as the draft model unchanged.
- Thinking budgets move from a server-wide flag to a per-request field, enforced inside speculative verification in batches.
- Tool-call parsing moves to mlx-vlm's parsers, inferred from the chat template with a per-request override: `qwen3_coder` for Qwen3.8 and Qwen3-Coder, `atem` for Muse Glimmer. Airlie's two parsers and the client-side parse of raw Muse output are deleted.
- Reasoning splitting moves to mlx-vlm's think-token handling; the `reasoning_parser` setting goes.
- Pictures and video go to the server as content parts; airlie keeps its frame sampling and picture scaling because they bound request size, not because the server refuses video.
- Time-to-first-token is measured by the client as time to the first streamed delta; the server-side stamp goes.
- The busy probe reads mlx-vlm's metrics endpoint instead of a patched status endpoint.

## Team shapes as workflow nodes, with no restart

Noah, 2026-11-08: "if we're doing a reconstruction of the engine, can we make it so workflows can access k-independent teams with shared memory, supervisor with workers, solo with fork, etc as nodes in a workflow. So it can't require a full system reboot like we have now."

Today a topology (`single`, `teams`, `fork`, `studio`) is chosen once per process with `--topology`, and `task restart -- --topology studio` restarts the engine because each profile also carries engine flags: KV size, cache quantization, cache memory, the server-wide tool parser. The story line then refuses to run unless the engine's command line matches the story profile (`engine_matches_profile`, the D10 gate). Two things force the restart, and this design removes both.

**Engine flags per profile.** With mlx-vlm the tool parser and the thinking budget are per request, so the only server-level knobs left are KV size, KV bits and the draft model. Profiles stop overriding engines: the studio values become the base engine configuration in `airlie.yaml`, every shape runs on the same engines, and the story line's profile gate shrinks to "engine healthy and its KV at least what the story needs", checked by the doctor when the engine starts. `--topology` leaves `task restart`.

**Topology as a process-wide class.** `build_topology` picks one Python class per process. The shapes become node types on the surface nodes-are-config builds, so a card runs whatever its workflow draws and several cards with different shapes run at once on the same engines:

| Shape today | Node type | Configuration, no code | Protocol code behind it |
| --- | --- | --- | --- |
| studio's per-layer supervisor | `team` (in nodes-are-config) | supervisor and worker roles, worker template, cap | nodes-are-config's team runner |
| `teams` (k independent teams) | `teams` | `k`, role, steps, token budget, `stop_on_first_pass`, `memory: shared` | the bundle runs k times in parallel, each in its own session and worktree, all k reading and writing one Learning to Share memory bank keyed by the node run, its learned controller admitting the steps worth sharing; the first green hand-back wins and the rest are cancelled (`teams/native_team.py`, `teams/aggregate.py`) |
| `fork` (solo, fork when stuck) | a `conversation` node with a `fork` policy | `after_failures`, `width`, `diversify: hypotheses or temperature`, `child_steps`, `max_rounds`, `resolve` | `topology/fork.py` and `topology/diversify.py`: the same node's bundle run width-wide with the nudge, children resolved back into the parent |
| `studio` (chief, router, specialists) | not a node | it is the story workflow itself on this surface | the story workflow |
| `single` | a plain `conversation` node |  |  |

Where it lands. The engine half, profiles losing their engine flags and the gate shrinking, is phase 4 of this plan. The node types are flow-layer work on nodes-are-config's schema v2 and runners, so they are proposed as nodes-are-config plan 3, "topologies are nodes", after plan 1 merges, and not handed to the engine implementer. The engine's part of the bargain is concurrency: k teams times their workers all land on one engine, bounded by `max_num_seqs` and the KV budget, and the validation gate's concurrency test proves it with a k-of-3 teams load sent straight to the engine.

Shared memory is the Learning to Share model ([Fioresi et al., 2026](https://arxiv.org/abs/2602.05965)): the k teams read and write one global memory bank of intermediate steps, scoped to the node run, and a learned controller decides which steps are admitted. Airlie's long-term store and its learned admission controller already serve that bank, so the teams node type is a config change, not a new sharing protocol. The teams do not share a conversation; D25 of nodes-are-config stands.

## The mlx\_vlm engine kind

An `EngineSpec` of kind `mlx_vlm` names one model and the serving knobs. The existing fields keep their names where mlx-vlm has the same knob; fields that only made sense for vllm-mlx go.

| airlie.yaml field | mlx-vlm flag or environment | Note |
| --- | --- | --- |
| `model` (one name, replaces `models: [...]`) | `--model <snapshot path>` | preloaded at start; the request body always names this model |
| `port`, `host`, `api_key` | `--port`, `--host`, `--api-key` | `/health` and `/v1/metrics` need the same key |
| `max_num_seqs` | `--max-num-seqs` | continuous batching is always on |
| `max_kv_size` | `--max-kv-size` |  |
| `kv_bits` (new, replaces boolean `kv_cache_quantization`) | `--kv-bits` | batching with a quantized cache is supported |
| `prefill_step_size` | `--prefill-step-size` |  |
| `mtp: true` on the model spec | `--draft-model <head snapshot> --draft-kind mtp` | the head is used as published; `engine/mtp.py` goes |
| `prefix_cache: true` (new) | `APC_ENABLED=1`, `APC_DISK_ENABLED=0` | memory tier only; the disk tier is off until measured |
| `cache_memory_gb` (new, replaces `--cache-memory-mb` in `extra_args`) | `APC_MEMORY_MAX_GB` | per process; main 8, reviewer 4, small 1, about today's 12 in total; the doctor's projection reads it |
| `cache_checkpoints` (new, default 8 on main, 4 elsewhere) | `APC_CHECKPOINT_ENTRIES` | the generalist's recurrent cache and Muse's rotating cache use the checkpoint path, not blocks; mlx-vlm's default of 2 would miss turns once several conversations share one process |
| `cache_checkpoint_interval` (new, default 2048) | `APC_CHECKPOINT_INTERVAL_TOKENS` |  |
| `buffer_cache_mb` | `MLX_BUFFER_CACHE_LIMIT` | same environment variable airlie sets today, set on every process |
| `max_image_px` (attachments) | `--max-pixels` | mirrors the client-side scaling bound |
| `extra_args`, `env` | appended verbatim | the escape hatch |

Gone from `EngineSpec`: `continuous_batching`, `use_paged_cache`, `memory_budget_gb`, `server_parsers`, `per_request_draft`, `kv_cache_quantization`. Gone from `ModelSpec`: `reasoning_parser`, `preload` (every engine preloads its one model). `tool_call_parser` stays and takes mlx-vlm names: `qwen3_coder` for the generalist, `atem` for the reviewer while it is Muse Glimmer (another reviewer model takes its own parser name), unset for the rest so the server infers from the chat template.

Launch, from `engine/registry.py`, for the main engine under the studio profile:

```
APC_ENABLED=1 APC_DISK_ENABLED=0 MLX_BUFFER_CACHE_LIMIT=8192M \
uv run --quiet python -m mlx_vlm.server \
  --model <hf-cache>/mlx-community/Qwen3.8-27B-4bit/snapshots/<rev> \
  --draft-model <hf-cache>/mlx-community/Qwen3.8-27B-MTP-bf16/snapshots/<rev> --draft-kind mtp \
  --host 127.0.0.1 --port 8080 --max-num-seqs 16 --max-kv-size 131072 \
  --kv-bits 8 --prefill-step-size 2048 --max-pixels 2359296
```

Process management stays in `engine/process.py`: detached spawn under `uv run`, log and pidfile in the state directory, SIGTERM then SIGKILL. Readiness probes `GET /health` until it answers with the model loaded, then `GET /v1/models` shows the entry with `loaded: true`. No airlie entry point wraps the server; `vllm_mlx_serve.py` is deleted.

The doctor's `engine_matches_profile` compares the running command line's `--max-num-seqs`, `--max-kv-size`, `--kv-bits` and `--draft-model` with the configuration, finding the process by `mlx_vlm.server`. Its memory projection changes in one place: the prefix-cache budget is read from each engine's `cache_memory_gb` instead of parsing `--cache-memory-mb` out of `extra_args`, and summed across processes with weights and KV against the wired limit.

## Client changes in airlie

`HTTPEngine` stays the one OpenAI-compatible client; it learns the mlx-vlm dialect and stops sending vllm-mlx's. Both dialects are tolerated during the transition, so phases 2 and 3 are tested against either engine while the committed config still points at vllm-mlx; the two engines are never resident at the same time.

| Concern | Today (vllm-mlx) | With mlx-vlm |
| --- | --- | --- |
| thinking on or off | top-level `enable_thinking` plus `chat_template_kwargs.enable_thinking` | top-level `enable_thinking` only |
| thinking budget | `thinking_token_budget` | `thinking_budget` |
| chat template arguments | `chat_template_kwargs` passed through, the reviewer roles send `reasoning_strength: low` | no passthrough; the client maps `reasoning_strength` to `reasoning_effort`, which mlx-vlm also renders as `reasoning_strength` for Muse; any other key is a config error |
| tool parser | server-wide `--tool-call-parser` | per-request `tool_parser` from the model spec, or omitted |
| reasoning in the reply | `reasoning_content` | `reasoning_content`, with `reasoning` as a deprecated alias; the client reads either |
| tool calls in the stream | de-duplicated by id; finished calls re-sent under a new id | plain OpenAI deltas keyed by `index`; the repair code in `autogen_client.py` and `serve/app.py` stays until the gate shows it idle, then goes |
| cached prompt tokens | `[mllm_cache]` log line; `cached_tokens` always 0 | `usage.cached_tokens`; the metric is read, not scraped |
| MTP counters | `generation_metadata.mtp_accepted` and `mtp_drafts` | `usage.draft_n` and `usage.draft_n_accepted` |
| time to first token | `airlie_ttft_ms` stamped by a patch | measured by the client at the first content delta |
| engine failure | `finish_reason: error` → `EngineError` | HTTP error status → `EngineError`; `finish_reason` keeps the length and tool\_calls checks |
| busy probe (`serve/bridge/busy.py`) | `/v1/status` with patched `active_requests` | `/v1/metrics` `summary.in_flight`, sent with the engine's key; 404 still means idle |
| health | `/health` and `/v1/models` | same paths; both `/health` and `/v1/metrics` need the management key when one is set |

Files touched: `engine/http_engine.py`, `teams/autogen_team.py`, `teams/autogen_client.py`, `serve/app.py`, `serve/bridge/busy.py`, `config/schema.py` (validators that name `thinking_token_budget`), and `prompts/steering/reasoning-carried.md`, which explains the engine's handling of past reasoning and must be re-checked against mlx-vlm's template behaviour.

Wire fixtures: `tests/fixtures/vllm_mlx/*` are replaced by `tests/fixtures/mlx_vlm/*` recorded from a real 0.7.12 server with `scripts/record_fixtures.py`: plain, reasoning, tool call, resume, error, health, models, metrics. The unit tests that drive the client against fixtures stay; the two that import vllm-mlx's pydantic request model to check field names become a fixture assertion.

## What gets deleted

All of it in the last phase, after the gate, in one pull request so the diff is reviewable as "vllm-mlx and llama-server leave".

- Code: `src/airlie/engine/vllm_mlx_serve.py`, `src/airlie/engine/patches/` (all four), `src/airlie/engine/parsers/` (both parsers and the registration module), `src/airlie/engine/mtp.py`, the vllm-mlx branch of `engine/registry.py` and `engine/process.py`, the parser-registry use in `engine/output_parse.py`.
- Tests: `test_vllm_mlx_serve.py`, `test_mtp_batch_join.py`, `test_hybrid_prefix_cache.py`, `test_active_requests.py`, `test_first_output.py`, `test_qwen3_xml_raw.py`, `test_muse_parsers.py`, `test_engine_mtp.py`, `tests/unit/vllm_helpers.py`, the Muse cases in `test_output_parse.py` and `test_autogen_client.py`, the MTP-directory case in `test_process.py`, and the `MAC_ONLY` and `MAC_ONLY_TESTS` entries in `tests/unit/conftest.py` that name them. `test_learned_controller.py` stays mac-only because it imports mlx.
- Dependencies: `vllm-mlx==0.5.0` and `mlx-lm==0.32.0` leave `pyproject.toml`; `mlx-vlm==0.7.12` arrives; `mlx>=0.32.4`. The trial lock resolved cleanly on 2026-11-08 with mlx 0.32.4, transformers 5.19.0 and mlx-audio 0.6.4 pulled in.
- Config and docs: the llama\_server kind and Kev's engine entry, the coder8, llama70b and qwen25\_32b model entries, the studio-coder8 and studio-qwen25 profiles, task bench:models, the `vllm_mlx` kind in `EngineSpec`, the `--models-config` registry YAML, the `server_parsers` and chunked-prefill comments in `config/profiles.yaml`, the `[mllm_cache]` references in `doctor.py` and `README.md`, the `task engine:install` text the doctor prints for a task that does not exist, the mutation-testing `mac_functions` entries for the deleted files, and `docs/bench/README.md`'s note on parser flags.
- Fixtures: `tests/fixtures/vllm_mlx/`.

Kept on purpose: `serve/uploads.py` frame sampling and picture scaling (they bound request size), `tools/jsonargs.py` repair (a model can still emit bad JSON), the `ENGINE_RETRIES` stall retry in `native_team.py` (cheap insurance against any engine), and the `thinking_budget` versus `max_tokens` validators.

## Validation gate

The old engines are deleted only when every row below passes on Noah's Mac. The gate is run by the implementer with the results pasted into the pull request; Noah reads them before the delete phase starts. Today's workflows do not complete, so no workflow run is the bar: each row is a feature test, run on vllm-mlx first (in phase 3, while it is still the configured engine) and on mlx-vlm second, and the two results sit side by side in the Feature comparison below. The two engines cannot be resident at once (the vllm-mlx registry's weights and cache beside the mlx-vlm processes exceed the wired limit), so the mlx-vlm half runs with the vllm-mlx engine stopped, on an idle line.

| Check | Pass means |
| --- | --- |
| Unit and web gates | `uv sync --frozen`, ruff, mypy, pytest, `task web:gate` green with the mlx\_vlm engines configured |
| Live engine tests | `tests/live/test_engine_live.py` and `test_parsers_live.py` pass against the main and reviewer engines: health, streaming, reasoning split, native tool call, yes/no, second turn faster |
| Text-only models load | `small` starts and answers; this checks upstream issue 1958 is fixed in the pinned release |
| Attachments | one picture and one video through chat reach the generalist and are described correctly |
| Reviewer | a review round on the reviewer engine returns a parsed verdict and tool calls with no client-side parsing; the seat is any adversarial model from a different lab than the coder's, Muse Glimmer through the `atem` parser today |
| Prefix cache hits | on the second turn of a conversation, `usage.cached_tokens` is greater than zero on the generalist (recurrent cache) and on the reviewer (rotating cache), while four other conversations are in flight on the same process |
| MTP | the generalist decodes with the draft model loaded; `usage.draft_n_accepted` over `usage.draft_n` reported per run |
| Wall clock | the same prompts on both engines: prefill and decode tokens per second and time to first token, at 1, 4 and 16 concurrent requests; mlx-vlm not worse than vllm-mlx on the same hardware |
| Concurrency | a new concurrency test sends 1, 4, 8 and 16 parallel streaming requests straight to the main engine, plain, tool-call and MTP-drafted mixed, with newcomers joining mid-decode: every stream matches its solo run at temperature 0, no interleaved or corrupted deltas (the failure PR 181 fixed), aggregate and per-stream tokens/s recorded |
| Decision model | airlie's router questions through Kev on llama-server and through Clef-flash and decider-2b on mlx-vlm, same inputs: the winner matches Kev's choices and confidences |

The prefix-cache and MTP rows are run twice because mlx-vlm does not yet combine batched prefix caching with speculative decoding ([issue 1471](https://github.com/Neywa-Labs/mlx-vlm/issues/1471)): once with `prefix_cache: true, mtp: false` and once with `prefix_cache: false, mtp: true`. The wall-clock row decides which becomes the studio default; the other stays available in config. If neither matches vllm-mlx, the work stops at phase 3 and Noah decides.

The concurrency test also sends a k-of-3 teams load: three groups of parallel requests sized like a teams node's workers, sent straight to the engine. No story, pipeline or workflow runs in any gate test; those need their own cleanup.

### Feature comparison

What the swap gains and loses, feature by feature. Each row is what mlx-vlm 0.7.12's source and docs say; the gate runs confirm or overturn it.

| Feature | vllm-mlx today | mlx-vlm | Gained or lost |
| --- | --- | --- | --- |
| Prefix reuse | airlie patch rewrites two private methods | built-in cache for recurrent and rotating layers | gain: no patch, cached\_tokens reported |
| MTP speculation | rebuilt model directory plus batch-join patch | native drafter, head used as published | gain: no patch; loss: cannot run with prefix cache in batches (issue 1471) |
| Batching | works with airlie's PR 181 guard | always on | same, if the concurrency row shows no corruption |
| Thinking budgets | one setting for the whole server | set per request | gain |
| Tool-call parsing | two airlie parsers, one from an unmerged PR | built-in qwen3\_coder and atem | gain: nothing to maintain, and if mlx-vlm's qwen3\_coder parser is clean, Qwen3-Coder can come back to raise token output; risk: Qwen3-Coder defects may return |
| Pictures and video | supported | supported | same |
| KV quantization | on or off | bit count, default 8 | gain: finer control |
| Time to first token and busy probe | two patches | client timing and /v1/metrics | same, no patches |
| Models per process | six in one process | one per process | loss: three processes, plus about 2.3 GB duplicate weights with MTP (issue 2508) |
| Text-only models | supported | fixed on main, unverified in 0.7.12 (issue 1958) | unknown until phase 1 |
| Kev decisions | llama-server | mlx-vlm decision endpoint | gain: one server kind, if a model matches Kev |
| Speed | baseline | not yet measured | decided by the gate's wall-clock row |

## Phases

Each phase is one pull request, green on the full test gate, and leaves the current engine working until phase 5. The implementer stops after the gate run (phase 4b) and waits for Noah's read of the results before phase 5. The gate run happens on Noah's Mac, on an idle line, with the models present.

1. **Prerequisites.** Rebase on main after the nodes-are-config merge. Re-check PyPI for the latest mlx-vlm release and pin it (0.7.12 when this was written) beside the existing pins in `pyproject.toml`, with `mlx>=0.32.4`, and re-lock. Start mlx-vlm's server by hand on port 8094 (8090 is `task serve`'s) with the `small` model and confirm a text-only checkpoint loads and answers; if it does not, stop and report (upstream issue 1958). Record the wire fixtures listed under Client changes from that server. Proves: the dependency resolves on this machine and the server answers.
2. **The engine kind.** `EngineSpec` gains kind `mlx_vlm` with the field table above; `engine/registry.py` builds its command line and environment; `engine/process.py` starts, probes and stops it; the doctor recognises it and reads `cache_memory_gb` into its projection. The `vllm_mlx` and `llama_server` kinds are untouched. Unit tests pin the argv and environment the way `test_registry.py` pins them today. Proves: `airlie engine up` starts an mlx\_vlm engine from config and `airlie engine status` sees it healthy.
3. **The client dialect.** `HTTPEngine` and the three callers send `thinking_budget`, per-request `tool_parser` and `reasoning_effort` in place of `chat_template_kwargs`, read `reasoning_content` or `reasoning`, time the first delta, map HTTP errors to `EngineError`; the busy probe reads metrics with the engine's key. Tests run against both fixture sets. Proves: the client passes the live engine tests and the concurrency test against an mlx\_vlm engine, while the committed config still points at vllm-mlx.
4. **Switch the config (4a), then run the gate (4b).** 4a is a pull request: `airlie.yaml` describes the main, reviewer, small and decision engines as mlx\_vlm with the studio values as the base; `config/profiles.yaml` keeps only role and team settings; the story line's profile gate becomes the engine-health and KV check; `--topology` leaves `task restart`; `task up` starts the four engines. The production line keeps running on vllm-mlx until Noah restarts it. 4b is a scheduled run, not a pull request: with the line idle and the vllm-mlx engine stopped, run every row of the validation gate twice as described (prefix cache on with MTP off, then the reverse) and the decision-model comparison, and paste the results into the 4a pull request. Proves: the gate.
5. **Delete vllm-mlx and llama-server.** Everything under What gets deleted, in one pull request, after Noah's go. Proves: the full test gate is green with no `vllm_mlx` or `mlx_lm` import anywhere in the tree, `uv.lock` holds neither package, and no `llama_server` engine is configured.
6. **Topologies are nodes, separate plan.** The `teams` and `fork` node types on nodes-are-config's surface, as its plan 3, after its plan 1 merges. Not part of this implementer's hand-off.

The detailed task list for phases 1 to 5, with file paths, test names and the exact commands, is written as a superpowers plan in `docs/superpowers/plans/` once this document is approved. That plan, not this document, is what the implementing model receives.

## Risks and upstream issues to track

| Risk | What we know | Mitigation |
| --- | --- | --- |
| Prefix cache and MTP cannot combine in batches | [issue 1471](https://github.com/Neywa-Labs/mlx-vlm/issues/1471), open since 2026-06-30 | the gate measures both configurations; the loser stays one config line away |
| Text-only checkpoints refused by the server | [issue 1958](https://github.com/Neywa-Labs/mlx-vlm/issues/1958); maintainer says fixed on main, unverified in 0.7.12 | phase 1 checks it first; fallback is a pinned git revision |
| Qwen3.8-27B with MTP holds a duplicate of some projection weights | [issue 2508](https://github.com/Neywa-Labs/mlx-vlm/issues/2508), about 2.3 GB at 4-bit | fits the budget; noted in the doctor's projection |
| Upstream churn | four releases between 2026-10-19 and 2026-11-04; 262 open issues | exact version pin; upgrades are their own small pull request with the live tests as the check |
| One resident model per process | by design in mlx-vlm's server | one process per preloaded model; three processes replace one |
| Disk prefix-cache tier writes to disk by default | `APC_DISK_ENABLED` defaults on | off in phase 2; revisit if cold starts matter |
| Client-side repairs mask a parser difference | the Qwen3-Coder parser defects from the 2026-10-14 bench may or may not exist in mlx-vlm's `qwen3_coder` | the live parser tests check shifts, comparisons and tool markup inside values; failures are filed upstream, not patched |
| Past reasoning in the chat template | airlie carries reasoning back as plain text because vllm-mlx's template dropped it | re-read `reasoning-carried.md` against mlx-vlm's behaviour in phase 3 |

Issues airlie never filed upstream, which should be filed in whichever project they now belong to: the batched-MTP join corruption, the four Qwen3-Coder parser defects with the bench repro, and the Muse Glimmer ATEM parsers now that mlx-vlm has them natively (vllm-mlx PR 812 stays open).

## Replacing Kev

Kev moves to mlx-vlm in this work, and llama-server is deleted with vllm-mlx in phase 5. Kev-4B is a LoRA adapter and decision head on Qwen3.5-4B-Base, published only as the adapter and ggml-org's GGUF, and mlx-vlm has no Kev family. Its typed-decision endpoint (`POST /v1/decisions`) takes the same request shape as llama.cpp's `/v1/systemone`: a state plus named questions with a type, instructions and criteria.

Every decision family mlx-vlm serves is the same concept Jev introduced, so the dynamic property Noah chose Kev for is not lost by switching. If one matches Kev on airlie's router questions, llama-server goes too and the engine side is one server.

| Family | Base | Size | Public score against Kev |
| --- | --- | --- | --- |
| Clef-flash | Cloudflare, Qwen3.8 finetune | 9B | above Jev and well above Kev-9B on Cloudflare's Decision Index run; official mlx-community weights |
| decider-2b | Mapika, Qwen3.5-2B-Base | 2B | JevBench hard tier 0.595 against Kev-4B's 0.541 on its own card |
| D1-2B | Liquid AI, LFM3-VL | 2B | positioned on latency and vision; official MLX weights |
| Laya | ConvAI encoder | small | trails every other model in Cloudflare's table |

In phase 4, beside the engine gate: run airlie's router questions through Kev on llama-server and through Clef-flash and decider-2b on mlx-vlm's decision endpoint, on the same inputs, and compare choices and confidences. `router/jev.py` changes by a path and two field names if one wins. If neither matches Kev, Kev stays on llama-server and Noah decides.

## Calls made here, and questions for Noah

Calls the author made, which Noah can overturn by editing the section they live in:

- A process per preloaded model (main, reviewer, small) rather than pointing the summarizer at the generalist to save a process. The small model costs about 3 GB and keeps summaries off the busy engine.
- The disk tier of the prefix cache is off until measured.
- KV quantization becomes a bit count, default 8, instead of a boolean.
- The client-side tool-call repairs stay through the gate and are removed only when the gate shows them idle.
- Kev moves off llama-server in the same work: the decision comparison is a phase 4 gate row, and llama-server is deleted with vllm-mlx in phase 5.
- The version is pinned exactly at 0.7.12, and the nodes-are-config merge is a prerequisite rather than a parallel track, because both edit `config/schema.py`.

Calls added after the builder check (a fresh engineer read the document on 2026-11-08 and asked four questions; two were Noah's and are now settled, two are settled here):

- Each engine's prefix cache is sized in config instead of left to mlx-vlm's defaults. `cache_memory_gb` (main 8, reviewer 4, small 1) caps the memory the cache may use. `cache_checkpoints` (8 on main, 4 elsewhere) sets how many conversation snapshots it keeps. Today's coder and reviewer models resume a conversation from a snapshot, and mlx-vlm keeps only two by default, so with several conversations in flight most turns would start cold. The gate's prefix-cache row checks this with four other conversations running. The doctor adds `cache_memory_gb` to its memory projection, so the cache cannot push the machine into swap unnoticed.
- Phase 4 is a config-switch pull request (4a) and a separately scheduled gate run on an idle line (4b), because the two engines cannot be resident at once and the gate occupies the GPU for hours.
- The bench models (coder8, llama70b, qwen25\_32b) and their studio-coder8 and studio-qwen25 profiles are dropped. The hand-started test server uses port 8094, since 8090 belongs to `task serve`.
- The client maps the reviewer roles' `reasoning_strength` to mlx-vlm's `reasoning_effort`; mlx-vlm has no generic chat-template passthrough, and any other key in `chat_template_kwargs` becomes a config error rather than a silent drop.

Questions only Noah can answer: none open; the last ones were settled in comments on 2026-11-08.
