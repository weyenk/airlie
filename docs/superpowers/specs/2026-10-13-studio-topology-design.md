# Studio topology — design

> **Status:** draft for Noah's review (2026-10-13). Nothing built yet.
> **Lineage:** the "chief + Jev router + specialists" idea from `~/git/ai-mode`, rebuilt on airlie's seams.
> **Relation to the build spec:** a fourth topology next to `teams` (spec), `fork` and the single agent; the
> spec's components (engine, roles, worktrees, event log, benches) are reused unchanged. LTS is not part of it.

## 1. Problem

ai-mode had the right shape and the wrong economics: one generalist (chief) talked to the human and routed
work through a decision model (Jev) to specialists, each with its own prompt and knowledge. But it ran nine
mostly-unique models on one GPU, split along lines that did not need splitting (chief, fast, coder, coder-xl,
product, research, docs, …), and every specialist was a one-shot chat rather than an agent with tools and a
test loop. airlie now has the agent loop, worktrees, a multi-model engine and benches; it lacks the delegation
layer.

## 2. Goals and non-goals

Goals
- One conversation partner (chief) that scopes work, delegates to specialist agents, and synthesises.
- Specialists are **roles** = model + prompt + tools + skills + knowledge, declared in `airlie.yaml`.
- Routing by a calibrated decision model (Jev) with a measurable accuracy, or by chief itself; one config line.
- The fewest resident models that cover text, code and vision: three plus the router.
- Everything mlx-community except the router (Jev's weights exist only as GGUF for llama-server).
- Parallel only across *different* subtasks; a specialist is the single-agent loop the topology bench favoured.

Non-goals
- No learned component beyond Jev. No cross-team memory. No merging of parallel code edits (coder subtasks
  serialise on one worktree).
- No interactive back-and-forth inside a run in v1: chief states assumptions instead of asking, and `serve`
  returns them with the result. A later version adds `needs_input`.

## 3. Models (profile `studio`, all mlx-community, pinned at build time)

| role(s) | model | size | why |
|---|---|---|---|
| chief, ux, design, product, docs, research | `Qwen3-VL-30B-A3B-Instruct-4bit` | 18.3 GB | one generalist that also sees screenshots; MoE with 3B active so it is fast; 256k context; standard attention (prefix cache works) |
| coder, qa, security | `Qwen3-Coder-30B-A3B-Instruct-4bit` | 17.2 GB | today's base; the only role family that uses native tool calls (vllm-mlx's `--tool-call-parser` is server-wide, so one tool-calling family per server) |
| summarizer, hypothesizer, triage | `Qwen3-4B-4bit` | 2.3 GB | cheap helpers |
| router | `ggml-org/Kev-4B-GGUF` on llama-server `/v1/systemone` | 2.5 GB | calibrated closed-set decisions; the one non-mlx-community exception, flagged; `router.kind: chief` needs no second server |

Resident weights ≈ 38 GB + Jev 2.5 GB, leaving ~50 GB of the 90 GB budget for KV. Chief and specialists are
*prompts* over three models, not nine models: ai-mode's `product`, `docs`, `research`, `ux`, `design` collapse
onto the generalist; `qa` and `security` onto the coder.

Profile `studio-max` (the "best that fits" variant, benchmarked before it becomes default): coder =
`GLM-4.5-Air-4bit` (60.1 GB, standard attention, 12B active) or `gpt-oss-120b-MXFP4-Q4` (62.3 GB, harmony tool
format), generalist = `Qwen3-VL-8B-Instruct-4bit` (5.8 GB), helpers unchanged ≈ 70 GB. Excluded on purpose:
`Qwen3-Next-80B`, `Qwen3-Coder-Next`, `Qwen3.5-35B-A3B` are hybrid-attention and vllm-mlx 0.5.0 cannot prefix-cache
them, which the M1 bench showed makes every agent turn re-prefill (doctor already warns).

Engine: one vllm-mlx registry as today, `max_kv_size 262144`, plus a managed `llama_server` engine for Jev
(design 2026-10-13 "add jev" §1, unchanged). Vision input needs `Message.content` to accept OpenAI content parts
(`text` / `image_url`); the native loop and `HTTPEngine` gain that, the AutoGen loop does not (chief and the
vision roles run on the native loop).

## 4. The loop

```
request (goal, optional repo, optional images)
  chief.plan ──────────────► plan: [{id, role?, brief, depends_on[], inputs}], assumptions[]   (JSON protocol, §5)
  for each subtask in dependency order (independent ones in parallel, coder ones serialised on the worktree):
      role   = router.decide(brief, roles-with-summaries)         (Jev stage A | chief's own pick)
      skills = router.decide(brief, role's skills) if the role has a skill library   (Jev stage B)
      result = specialist.run(role, brief, preamble = repo map + skill text + prior results it depends on)
  chief.synthesise(plan, results) ──► final answer | one more round of subtasks (≤ studio.max_rounds)
report: final answer, per-subtask results, routing decisions with confidence, winning branch if code changed
```

- **Chief** runs on the native loop with no native tools and no worktree. It emits one JSON object per turn
  (`plan`, `synthesis`, or `delegate` for a single extra subtask); airlie parses it, repairs with the existing
  JSON-args repair, and on a second parse failure falls back to treating the text as the final answer.
- **Specialists** are `TeamContext`s: the role's prompt, the role's tool set (`roles.<r>.tools`, default none;
  coder keeps today's builtins + `run_tests`), the role's skills as preamble, `max_steps` per role. Coder
  subtasks share one worktree (`airlie/<run_id>/studio`) and run one at a time, so edits accumulate and the
  tests see everything; the branch is the deliverable as today. Non-coder specialists get `read_file`/`grep`
  on the worktree read-only when a repo is given.
- **Dependencies**: a subtask's `depends_on` results are appended to its brief (capped at
  `studio.result_chars`); the architect-style "map the repo first" pattern from ai-mode is just a first subtask
  everyone depends on.
- **Budgets**: `studio.max_subtasks` (12), `studio.max_rounds` (2), per-role `max_steps`, plus the run-level
  token budget. A specialist that fails (error, max_steps) is reported as failed to chief, which may retry it
  once with the failure appended or declare the outcome.

## 5. Chief protocol (what the generalist must output)

```json
{"type": "plan", "assumptions": ["the API is the Flask app in app/"],
 "subtasks": [
   {"id": "map", "role": "architect", "brief": "Map app/ and tests/: modules, entry points, test command."},
   {"id": "fix", "role": "coder", "brief": "Fix the 500 on POST /orders when qty is 0; add a regression test.", "depends_on": ["map"]},
   {"id": "ux",  "role": "ux", "brief": "Critique the attached checkout screenshot: friction, copy, layout.", "inputs": ["img:1"]}
 ]}
{"type": "synthesis", "answer": "…", "open_questions": ["…"]}
```
`role` is a hint: with `router.kind: jev` the router decides and the hint only breaks ties below the confidence
floor; with `router.kind: chief` the hint is the decision. The chief prompt is ai-mode's `chief.md` cut down:
single voice, delegate don't do, at most N read-only calls, name assumptions.

## 6. Router

`router.kind: jev | chief`.
- **jev**: `POST /v1/systemone` with `state` = brief (+ one line of goal context), question `role` of type
  `choice`, criteria = `{role: summary}` for every role that has a `summary:` in its prompt frontmatter (ai-mode's
  convention, so `chief`, helpers and deprecated roles are excluded automatically). Decision policy from ai-mode:
  `clear` (gap to runner-up ≥ `router.ambiguous_gap` 0.15 and confidence ≥ `router.min_confidence` 0.5) → that
  role; `ambiguous` → chief's hint if it is one of the top two, else top choice; `low-confidence` → chief's hint.
  Stage B (`skill` question) runs only for roles with a skill library.
- **chief**: the plan's `role` field, validated against the role list.
Every decision is an event `route {subtask, question, choice, probabilities, confidence, decision}` and a line
in `runs/<id>/routing.jsonl`, which `task route:eval -- evals/routing.jsonl` scores the way ai-mode's
`classify --eval` did (accuracy, mean confidence, wrong-but-clear, confusions). ai-mode's `evals/classify*.jsonl`
are the seed set, re-labelled to airlie's role names.

## 7. Skills and knowledge

- `skills/<role>/<name>.md`, each with `summary:` frontmatter. A role with a skill directory gets stage-B
  routing; the chosen skill's text (top two when ambiguous, as ai-mode did for QA layers) is injected as the
  specialist's preamble after the repo map. ai-mode's `qa/guidelines/*` is the first library, vendored as
  `skills/qa/`.
- `knowledge/<role>/**` (markdown, text): a `search_knowledge(query, k)` tool per role backed by BM25 over
  chunks (`rank-bm25`, no embeddings, no index service); results are plain text with the source path. This is
  the RAG seam; an embedding backend (`--embedding-model` on vllm-mlx) is a later swap behind the same tool.
- Both directories are declared per role (`roles.<r>.skills`, `roles.<r>.knowledge`), default none, so a role
  without them is exactly today's role.

## 8. Topology profiles (shared with single/teams/fork)

`topology.profiles.<kind>` is a partial config deep-merged over the base when that kind is selected, so each
topology declares its own resident model set, contexts and budgets:

```yaml
topology:
  kind: single
  profiles:
    single: { teams: { k: 1 }, lts: { enabled: false }, roles: { coder: { max_context: 262144 } } }
    teams:  { teams: { k: 3 }, lts: { enabled: true },  roles: { coder: { max_context: 65536 } } }
    fork:   { roles: { coder: { max_context: 131072 } } }
    studio: { roles: { chief: { model: generalist }, coder: { model: coder_30b } }, router: { kind: jev } }
```
The engine runs at the largest `max_kv_size` any profile needs; `task doctor` projects memory per profile
(K or parallel subtasks × context × the model's KV cost) and refuses a profile that does not fit. `--topology`
and `POST /v1/airlie/run {topology}` pick the profile for one run, exactly as today.

## 9. Configuration (new keys only)

```yaml
topology: { kind: studio, profiles: { … } }
router: { kind: jev, role: jev, ambiguous_gap: 0.15, min_confidence: 0.5 }
studio: { chief_role: chief, max_subtasks: 12, max_rounds: 2, parallel: 3, result_chars: 6000 }
roles:
  chief:     { prompt: prompts/roles/chief.md, model: generalist, tools: [] }
  architect: { prompt: prompts/roles/architect.md, model: generalist, tools: [read_file, list_dir, grep] }
  ux:        { prompt: prompts/roles/ux.md, model: generalist, vision: true }
  qa:        { prompt: prompts/roles/qa.md, model: coder_30b, tools: [read_file, write_file, edit_file, run_tests], skills: skills/qa }
  …
engines:
  decision: { kind: llama_server, port: 8081, models: [kev4b] }
```
`RoleSpec` gains `tools`, `skills`, `knowledge`, `vision`, `summary` (read from the prompt's frontmatter when
absent). `EngineSpec.kind` gains `llama_server` (from the Jev design). `Message.content` gains content parts.

## 10. Code shape

| piece | change |
|---|---|
| `config/schema.py`, `config/load.py` | `RouterSpec`, `StudioSpec`, `RoleSpec` fields, profiles deep-merge, `llama_server` engine kind |
| `engine/types.py`, `engine/http_engine.py`, `teams/native_team.py` | content parts (text + image_url) through the native loop |
| `engine/process.py`, `engine/registry.py` | `llama-server -m <pinned gguf> --host --port -c` command, `/health` readiness, `models:pull` of one gguf file |
| `router/{protocol,jev,chief,policy,log}.py` | `Router` protocol, `JevRouter` (httpx, mock transport in tests), `ChiefRouter`, decision policy, `routing.jsonl` |
| `skills/{library,knowledge}.py` + `tools/knowledge.py` | frontmatter summaries, skill injection, BM25 `search_knowledge` tool |
| `topology/studio.py` | chief protocol parsing, plan scheduling (dependency order, parallel ≤ `studio.parallel`, coder serialised), specialist runs, synthesis, report |
| `topology/protocol.py`, `cli.py`, `serve/app.py`, `Taskfile.yml` | `studio` kind, `task route:eval`, `task studio -- "goal" --image path` |
| `prompts/roles/*.md`, `skills/qa/*` | chief, architect, product, ux, design, docs, research, qa, security prompts with `summary:` frontmatter (ported and shortened from ai-mode); QA guidelines vendored |
| `bench/studio_bench.py`, `evals/routing.jsonl`, `evals/tasks/*` | routing eval; two multi-role tasks (a UI screenshot + code fix, a feature needing a plan) |

Events: `plan`, `route`, `subtask_start`, `subtask_done`, `synthesis`, `run_done`. Report: final answer,
assumptions, per-subtask table (role, decision, steps, outcome), branch.

## 11. Testing

- Unit: profile deep-merge and per-profile doctor projection; chief protocol parsing incl. repair and the
  text fallback; dependency scheduling (parallel set, coder serialisation, cycle = config error); `JevRouter`
  against a mocked `/v1/systemone` (clear / ambiguous / low-confidence / not-in-set); `ChiefRouter`; skill
  frontmatter + injection; BM25 search ordering and source paths; content parts on the wire; the studio run end
  to end on `FakeEngine` with scripted chief JSON and two specialists.
- Routing eval: `task route:eval` on the seed set ≥ 0.9 accuracy before studio is used for real work; the
  numbers in `docs/bench/`.
- Live: `task studio -- "…" --repo …` on inventory_cli (chief → architect → coder) must match the single agent's
  pass rate within noise and cost one extra generalist call; the two multi-role tasks graded by their own tests.

## 12. Success criterion

Studio is kept if, on the three graded coding tasks, its pass rate is not below the single agent's and its
wall-clock on solved runs is within 25 % of it (the overhead is one chief call and one routing call per
subtask), **and** routing accuracy on the seed set is ≥ 0.9. The multi-role tasks are the reason it exists and
have no single-agent baseline; they are graded pass/fail.

## 13. Open decisions for Noah
1. Generalist: `Qwen3-VL-30B-A3B` as chief *and* vision (recommended: one model, 18 GB, fast) vs a text-only
   generalist plus `Qwen3-VL-8B` for vision (24 GB together, two models to warm).
2. Router default: `jev` (needs llama-server + the one GGUF exception; calibrated, evaluable) vs `chief` (no
   second server; routing quality unmeasured). Recommended: `jev`, with `chief` as the fallback when the decision
   engine is down.
3. Whether to vendor ai-mode's prompts largely as-is (fast, proven) or rewrite them for the agent loop
   (specialists now have tools and tests, which ai-mode's one-shot prompts do not assume). Recommended: port
   chief and the summaries as-is, rewrite coder/qa/architect for tools.
