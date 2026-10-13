# Studio topology — design

> **Status:** draft for Noah's review, rev 2 (2026-10-13). Nothing built yet. Rev 2 after Noah's review: the
> generalist is Qwen3.8-27B, a cross-family adversarial reviewer on Meta's Muse Glimmer 30B is added (§4a, with
> the evidence), the coder seat gets a benchmarked alternative profile, and output parsing moves client-side on
> vllm-mlx's own parser classes, which lifts the one-tool-calling-family-per-server limit (§4b, verified live).
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
- The reviewer is from a different model family than the coder it reviews (§4a: same-family judges favour
  their own family's text).
- Parallel only across *different* subtasks; a specialist is the single-agent loop the topology bench favoured.

Non-goals
- No learned component beyond Jev. No cross-team memory. No merging of parallel code edits (coder subtasks
  serialise on one worktree).
- No interactive back-and-forth inside a run in v1: chief states assumptions instead of asking, and `serve`
  returns them with the result. A later version adds `needs_input`.

## 3. Models (profile `studio`, all mlx-community, pinned)

| role(s) | model | size | why |
|---|---|---|---|
| chief, architect, ux, design, product, docs, research | `Qwen3.8-27B-4bit` (`10c35caa…`, on disk) | 16.1 GB | the strongest model that fits and it sees images (mlx-vlm conversion, vision encoder included). Dense 27B, **hybrid attention** (48 linear + 16 full-attention layers), so vllm-mlx 0.5.0 cannot prefix-cache it; chief makes a few long one-shot calls (plan, synthesis), not forty cache-dependent turns, so the cost is bounded (one prefill per call). Thinking is switchable per request (`enable_thinking` in its template) and is on for plan/synthesis with a token budget. Specialists on it may use native tools (parsers `qwen` + `qwen3`, §4b). |
| coder, qa, security | `Qwen3-Coder-30B-A3B-Instruct-4bit` (`6e302ea6…`) | 17.2 GB | today's base: prefix cache works, 3B active so decode is fast; its XML tool format is parsed by vllm-mlx's `qwen3_coder` parser, run client-side under §4b. Profile `studio-qwen38` puts `Qwen3.8-27B` in this seat for the bench (below). |
| reviewer | `Muse-Glimmer-30B-4bit` (`3e7677d7…`, Meta, Apache 2.0) | 19.4 GB | the adversarial reviewer, deliberately **not Qwen** (§4a). Dense 30B, 52 layers of sliding-window (2048) + full attention, 2 KV heads, 131k context, vision encoder included. Loads in our env (mlx-vlm 0.7.6 and mlx-lm 0.32.0 both ship `muse_glimmer`). vllm-mlx 0.5.0 has no Muse parsers (PR #812 is open), so airlie vendors that PR's two parser classes and runs them client-side (§4b). |
| summarizer, hypothesizer, triage | `Qwen3-4B-4bit` | 2.3 GB | cheap helpers |
| router | `ggml-org/Kev-4B-GGUF` (Apache 2.0) on llama-server `/v1/systemone` | 2.5 GB | calibrated closed-set decisions; the one non-mlx-community exception, flagged; `router.kind: chief` needs no second server. "Jev" in this document names the System One API and decision policy; the weights airlie downloads are Kev (Jared Palmer's open clone, same API). TypeSafe's Jev itself is a closed hosted model; OpenJev is CC BY-NC and excluded. |

Resident weights ≈ 55 GB + Kev 3 GB = 58 GB. The ceiling is the GPU wired limit, 104 GB (`iogpu.wired_limit_mb
106496`), minus the 12 GB OS reserve: 92 GB for weights plus KV, so ~34 GB of KV headroom (one full 262k coder
context is ~24 GB at 96 KB/token; `task doctor` projects per profile, §8). `engines.main.memory_budget_gb` (90)
is a separate knob: vllm-mlx's cap on resident weights alone. Specialists are *prompts* over three models
plus one reviewer, not nine models: ai-mode's `product`, `docs`, `research`, `ux`, `design` collapse onto the
generalist; `qa` and `security` onto the coder.

**Why Qwen3.8-27B is the generalist and not (by default) the coder.** It is the best-scoring local model in its
class on vendor benchmarks (SWE-bench Pro 61.7, Terminal-Bench 2.1 73.0; independent reproductions are thin and
reviewers note it overthinks, ~2× tokens per task). But the M1 bench measured what hybrid attention costs an
*agent loop* on vllm-mlx 0.5.0: no prefix cache, so every turn re-prefills the whole conversation (4.3 s at a 4k
prefix vs 72 ms cached; prefill grows ~linearly, so ~35 s per turn at 32k, ~20 extra minutes on a 40-step solve),
and a dense 27B decodes several times slower than the 3B-active MoE. Those costs are per turn, which is why it
sits in the seat that makes few calls. Profile `studio-qwen38` (coder = `Qwen3.8-27B`, `thinking: true`,
`max_steps` unchanged) is a config line, and `task bench:topology -- --topology studio --profile studio-qwen38`
settles whether its higher per-step quality beats its per-turn cost on our tasks; the default moves if it wins.
Excluded on purpose for agent seats: `Qwen3-Next-80B`, `Qwen3-Coder-Next`, `Qwen3.5-35B-A3B` (same
hybrid-attention penalty, no quality claim that offsets it).

Dropped: the earlier `studio-max` profile (GLM-4.5-Air coder, 60 GB) no longer fits beside the generalist and the
reviewer (60 + 16 + 19 + 3 Kev + 12 reserve = 110 GB > 104, before any KV); it returns only if a benchmark asks
for it, with the reviewer swapped to `gpt-oss-20b-MXFP4` (12 GB, OpenAI family, parser already in vllm-mlx 0.5.0).

Engine: one vllm-mlx registry as today, `max_kv_size 262144`, plus a managed `llama_server` engine for Jev
(design 2026-10-13 "add jev" §1, unchanged). Vision input needs `Message.content` to accept OpenAI content parts
(`text` / `image_url`); the native loop and `HTTPEngine` gain that, the AutoGen loop does not (chief and the
vision roles run on the native loop). Muse Glimmer runs through vllm-mlx's MLLM path (auto-detected from its
config; `mllm: true` per registry entry exists if detection misses), and its sliding-window layers mean
vllm-mlx skips prefix reuse for it ("not trimmable"), which a one-shot reviewer never needs.

## 4. The loop

```
request (goal, optional repo, optional images)
  chief.plan ──────────────► plan: [{id, role?, brief, depends_on[], inputs}], assumptions[]   (JSON protocol, §5)
  for each subtask in dependency order (independent ones in parallel, coder ones serialised on the worktree):
      role   = router.decide(brief, roles-with-summaries)         (Jev stage A | chief's own pick)
      skills = router.decide(brief, role's skills) if the role has a skill library   (Jev stage B)
      result = specialist.run(role, brief, preamble = repo map + skill text + prior results it depends on)
      if role in studio.review.roles: review = reviewer.review(brief, diff, test_tail)   (§4a; one fix round on request_changes)
  chief.synthesise(plan, results, reviews) ──► final answer | one more round of subtasks (≤ studio.max_rounds)
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

## 4a. Adversarial review (cross-family)

After a coder-family subtask stops (visible pass, `done`, or `max_steps`), the `reviewer` role gets one
one-shot call: the brief, the worktree diff (capped at `studio.review.diff_chars`), the test tail, and the
touched files' current text (capped). It uses no tools and returns JSON:

```json
{"verdict": "request_changes", "findings": [
  {"severity": "blocking", "file": "app/orders.py", "line": 41, "claim": "qty == 0 still reaches the division",
   "evidence": "the guard checks `qty < 0`; the test only covers negative qty"}]}
```
`blocking` findings with `verdict: request_changes` give the coder **one** bounded fix round
(`studio.review.rounds` 1, reusing the fork work's `nudge` + resumed state); non-blocking findings and the
review itself travel to chief with the result either way. The reviewer never edits. Prompt: ai-mode's QA
stance (look for what is wrong, cite a line for every claim, say "no findings" when there are none) plus the
instruction that passing tests are not evidence the tests are adequate. Every review is an event `review
{subtask, verdict, blocking, findings, fix_round, tests_before, tests_after}` and a column in the studio bench.

**Why a different model family.** The evidence is on LLM-as-judge, not code review specifically, and it says
the bias is reduced, not removed, so this is a reasoned default that the bench column measures:
- Panickssery, Bowman & Feng 2024, *LLM Evaluators Recognize and Favor Their Own Generations*
  (arXiv 2404.13076): GPT-4 and Llama 2 identify their own outputs above chance, and after fine-tuning,
  self-recognition correlates linearly with self-preference; controlled experiments support a causal link.
- Wataoka, Takahashi & Ri 2024, *Self-Preference Bias in LLM-as-a-Judge* (NeurIPS 2024 SafeGenAI workshop,
  arXiv 2410.21819): judges rate lower-perplexity (more familiar) text higher than humans do, whether or not
  they wrote it. The mechanism is familiarity, so text from another family is the defence, not just another
  checkpoint of the same family.
- *MLLM-as-a-Judge Exhibits Model Preference Bias* (2025): the bias extends to the family (Qwen-based judges
  favoured Qwen-based generations in 9 of 12 cross pairs), and a cross-family ensemble of evaluators mitigated
  it without losing accuracy.
Qwen3.8-27B therefore cannot be the reviewer of Qwen3-Coder output, and Muse Glimmer (Meta) is the strongest
mlx-community model outside the Qwen family that fits beside the other two (gpt-oss-20b is the fallback).

**Muse Glimmer's output format.** Its template frames every turn as channels: generation begins
` to=self<|message|>…<|eom|>` (reasoning) and continues `to=user<|message|>…<|eot|>` (answer); later turns may
omit the `to=self` block and open with a bare `to=user`. vllm-mlx 0.5.0 would return all of that as `content`;
§4b parses it on the client with the parser classes from vllm-mlx PR #812. Reasoning depth is the template's
`reasoning_strength` (`medium` for reviews), passed as the per-request `chat_template_kwargs` airlie already
sends for `enable_thinking`.

## 4b. Output parsing: vllm-mlx's parsers, run client-side

vllm-mlx's reasoning and tool parsers are plain classes (`vllm_mlx.reasoning.get_parser(name)()`,
`vllm_mlx.tool_parsers.ToolParserManager.get_tool_parser(name)()`; tokenizer optional; `extract_reasoning(text)
-> (reasoning, content)` and `extract_tool_calls(text, request) -> {tools_called, tool_calls, content}`).
Verified in our env on 2026-10-13: `gpt_oss` split a Harmony string, `qwen3_coder` and `harmony` each parsed a raw
tool block into `{"name": "read_file", "arguments": "{\"path\": \"app.py\"}"}`, no server involved. About 20
tool formats and 7 reasoning formats are registered, and a model's names are exactly the server flag values.

**The server runs without parser flags.** Live check (same day, a throwaway `vllm-mlx serve` of Qwen3-4B with no
`--enable-auto-tool-choice` and no `--reasoning-parser`): a request with `tools` was rendered into the prompt,
the model answered with a tool call, `finish_reason` was `tool_calls`. The chat template gets `tools` whenever
the request carries them and `tool_choice != "none"`, independent of the parser flag (server.py `chat_kwargs["tools"]`). Without the
flag the server applies a *generic* fallback that recognises JSON-in-`<tool_call>`, Llama `<function=…>`,
MiniMax and Nemotron styles; Qwen3-Coder's XML, Harmony and Muse's atem channels come back raw in `content`.

**airlie's side.** `engine/output_parse.py`: `parse_output(model: ModelSpec, result) -> GenResult` runs after
every response: server-parsed `tool_calls` win; otherwise the model's `tool_parser` is applied to `content`, and
`reasoning_parser` splits reasoning out when the server returned none. Complete-output methods only; streaming
to the UI stays raw tokens, the loop acts on complete responses as today. `ModelSpec.tool_parser` /
`reasoning_parser` keep their names; `engines.<e>.server_parsers: false` (studio default) stops `registry.py`
turning them into server flags, `true` keeps today's behaviour (single/teams/fork keep `true` until the
topology bench shows parity on the coder). The Muse parsers are vendored from PR #812 as
`engine/parsers/vendor/muse_glimmer_{reasoning,tool}.py` (same base classes), registered at import under
`muse_glimmer`, deleted when a vllm-mlx release carries them. One wrapper, so a vllm-mlx rename costs one file;
vllm-mlx stays pinned (0.5.0) and the parser tests run on every `task check`.

**What this lifts.** The design no longer needs "one tool-calling family per server": Qwen3-Coder (`qwen3_coder`),
Qwen3.8 specialists (`qwen`/`qwen3`), Muse (`muse_glimmer`) and a future gpt-oss (`harmony`) can share one process.
Chief stays JSON-only because the protocol (§5) wants one object per turn, not because of parsing.

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
    studio: { roles: { chief: { model: generalist }, coder: { model: coder_30b }, reviewer: { model: reviewer } }, router: { kind: jev } }
    studio-qwen38: { extends: studio, roles: { coder: { model: generalist, thinking: true } } }
```
The engine runs at the largest `max_kv_size` any profile needs; `task doctor` projects memory per profile
(K or parallel subtasks × context × the model's KV cost) and refuses a profile that does not fit. `--topology`
and `POST /v1/airlie/run {topology}` pick the profile for one run, exactly as today.

## 9. Configuration (new keys only)

```yaml
topology: { kind: studio, profiles: { … } }
router: { kind: jev, role: jev, ambiguous_gap: 0.15, min_confidence: 0.5 }
studio: { chief_role: chief, max_subtasks: 12, max_rounds: 2, parallel: 3, result_chars: 6000,
          review: { enabled: true, role: reviewer, roles: [coder, qa, security], rounds: 1, diff_chars: 40000 } }
models:
  generalist: { repo: mlx-community/Qwen3.8-27B-4bit, revision: 10c35caafbb80f7dc6a7a432cdd11af10a6d4818,
                tool_call_parser: qwen, reasoning_parser: qwen3, tags: [hybrid-attention, vision] }
  reviewer:   { repo: mlx-community/Muse-Glimmer-30B-4bit, revision: 3e7677d7a40d348a3daba263a2b1c0aa41910710,
                tool_call_parser: muse_glimmer, reasoning_parser: muse_glimmer, tags: [vision] }
engines:
  main: { server_parsers: false }     # parsers run in airlie (§4b); the registry emits no parser flags
roles:
  chief:     { prompt: prompts/roles/chief.md, model: generalist, tools: [] }
  architect: { prompt: prompts/roles/architect.md, model: generalist, tools: [read_file, list_dir, grep] }
  ux:        { prompt: prompts/roles/ux.md, model: generalist, vision: true }
  reviewer:  { prompt: prompts/roles/reviewer.md, model: reviewer, tools: [], max_tokens: 2048,
               chat_template_kwargs: { reasoning_strength: medium } }
  qa:        { prompt: prompts/roles/qa.md, model: coder_30b, tools: [read_file, write_file, edit_file, run_tests], skills: skills/qa }
  …
engines:
  decision: { kind: llama_server, port: 8081, models: [kev4b] }
```
`RoleSpec` gains `tools`, `skills`, `knowledge`, `vision`, `summary` (read from the prompt's frontmatter when
absent) and `chat_template_kwargs`. `ModelSpec.tool_parser` / `reasoning_parser` are now applied client-side when
`EngineSpec.server_parsers` is false (§4b). `EngineSpec.kind` gains `llama_server` (from the Jev design).
`Message.content` gains content parts. Profiles gain `extends`.

## 10. Code shape

| piece | change |
|---|---|
| `config/schema.py`, `config/load.py` | `RouterSpec`, `StudioSpec`, `RoleSpec` fields, profiles deep-merge, `llama_server` engine kind |
| `engine/types.py`, `engine/http_engine.py`, `teams/native_team.py` | content parts (text + image_url) through the native loop; per-role `chat_template_kwargs` |
| `engine/output_parse.py`, `engine/parsers/vendor/muse_glimmer_{reasoning,tool}.py` | `parse_output(model, result)` over vllm-mlx's parser registries (server-parsed calls win, else the model's `tool_parser`, then `reasoning_parser`); the two Muse parser classes vendored from PR #812 and registered under `muse_glimmer` |
| `engine/registry.py`, `config/schema.py` | `EngineSpec.server_parsers` (studio: false) gates the `--enable-auto-tool-choice/--tool-call-parser/--reasoning-parser` flags |
| `topology/review.py` | `Reviewer.review(brief, diff, test_tail, files) -> Review{verdict, findings}` with JSON repair; fix-round wiring used by `topology/studio.py` |
| `engine/process.py`, `engine/registry.py` | `llama-server -m <pinned gguf> --host --port -c` command, `/health` readiness, `models:pull` of one gguf file |
| `router/{protocol,jev,chief,policy,log}.py` | `Router` protocol, `JevRouter` (httpx, mock transport in tests), `ChiefRouter`, decision policy, `routing.jsonl` |
| `skills/{library,knowledge}.py` + `tools/knowledge.py` | frontmatter summaries, skill injection, BM25 `search_knowledge` tool |
| `topology/studio.py` | chief protocol parsing, plan scheduling (dependency order, parallel ≤ `studio.parallel`, coder serialised), specialist runs, synthesis, report |
| `topology/protocol.py`, `cli.py`, `serve/app.py`, `Taskfile.yml` | `studio` kind, `task route:eval`, `task studio -- "goal" --image path` |
| `prompts/roles/*.md`, `skills/qa/*` | chief, architect, product, ux, design, docs, research, qa, security, reviewer prompts with `summary:` frontmatter (ported and shortened from ai-mode); QA guidelines vendored |
| `bench/studio_bench.py`, `evals/routing.jsonl`, `evals/tasks/*` | routing eval; two multi-role tasks (a UI screenshot + code fix, a feature needing a plan) |

Events: `plan`, `route`, `subtask_start`, `subtask_done`, `synthesis`, `run_done`. Report: final answer,
assumptions, per-subtask table (role, decision, steps, outcome), branch.

## 11. Testing

- Unit: profile deep-merge and per-profile doctor projection; chief protocol parsing incl. repair and the
  text fallback; dependency scheduling (parallel set, coder serialisation, cycle = config error); `JevRouter`
  against a mocked `/v1/systemone` (clear / ambiguous / low-confidence / not-in-set); `ChiefRouter`; skill
  frontmatter + injection; BM25 search ordering and source paths; content parts on the wire; the studio run end
  to end on `FakeEngine` with scripted chief JSON and two specialists; `parse_output` on one recorded fixture
  per format (Qwen3-Coder XML, Qwen3.8 bracket calls + `<think>`, Harmony, Muse channels from PR #812: leading-space
  ` to=self…<|eom|>to=user…<|eot|>`, a bare `to=user` turn, an unterminated `to=self` block) and the precedence
  rule (server-parsed `tool_calls` are not re-parsed); `registry.py` argv has no parser flags when
  `server_parsers` is false; review JSON parse + repair; the fix round
  (`request_changes` with a blocking finding resumes the coder once, `approve` does not, a second
  `request_changes` is reported not retried); profile `extends`.
- Routing eval: `task route:eval` on the seed set ≥ 0.9 accuracy before studio is used for real work; the
  numbers in `docs/bench/`.
- Live: `task studio -- "…" --repo …` on inventory_cli (chief → architect → coder) must match the single agent's
  pass rate within noise and cost one extra generalist call; the two multi-role tasks graded by their own tests.

## 12. Success criterion

Studio is kept if, on the three graded coding tasks, its pass rate is not below the single agent's and its
wall-clock on solved runs is within 25 % of it (the overhead is one chief call and one routing call per
subtask), **and** routing accuracy on the seed set is ≥ 0.9. The multi-role tasks are the reason it exists and
have no single-agent baseline; they are graded pass/fail. The review column reports, per task, findings,
blocking findings, fix rounds and whether the fix round changed the test outcome; the review must not lower
the pass rate, and its wall-clock (one reviewer call per code subtask, plus fix rounds) is part of the 25 %.

## 13. Decisions

Decided by Noah (2026-10-13): generalist = `Qwen3.8-27B` (chief + vision); an adversarial reviewer from a
different family, on Meta's `Muse-Glimmer-30B` (mlx-community 4-bit); mlx-community only, Jev the one exception;
output parsing reuses vllm-mlx's parser classes client-side and the one-family-per-server limit is lifted (§4b,
tools rendering without the parser flag verified live).

Still open:
1. Coder seat default: keep `Qwen3-Coder-30B-A3B` (cache-friendly, fast) and let `studio-qwen38` earn the seat
   in the bench (recommended), or make `Qwen3.8-27B` the coder from day one and accept the per-turn re-prefill.
2. Router default: `jev` (needs llama-server + the one GGUF exception; calibrated, evaluable) vs `chief` (no
   second server; routing quality unmeasured). Recommended: `jev`, with `chief` as the fallback when the decision
   engine is down.
3. Whether to vendor ai-mode's prompts largely as-is (fast, proven) or rewrite them for the agent loop
   (specialists now have tools and tests, which ai-mode's one-shot prompts do not assume). Recommended: port
   chief and the summaries as-is, rewrite coder/qa/architect for tools; the reviewer prompt is new.

## 14. Beyond code: the three seams a media studio needs

Studio is written for a dev shop, and three of its assumptions are dev-shop assumptions. They are named here so
the dev-shop implementations are built as one option of each seam, not as the only way; a photo/audio/video
studio is then a profile, a tool pack and a few prompts, not a fifth topology.

**Media models are tools, not roles.** Diffusion, speech-to-text, text-to-speech and upscalers are not chat
models: no conversation, no tool calling, and vllm-mlx does not serve them. The thing that reasons is still a
vision LLM (the generalist looking at a render and deciding what to change). So a media role is a vision LLM
holding tools such as `generate_image`, `transcribe`, `speak`, `upscale`, `cut`, each wrapping an MLX pipeline
(mflux, mlx-whisper, mlx-audio) or a CLI (ffmpeg). `ToolSpec` gains `kind: builtin | cli | model` and, for
`model`, `memory_gb`, `load`, `unload`; the tool registry is otherwise unchanged.

| seam | dev shop (built first) | media shop (later profile) |
|---|---|---|
| `Workspace` | git worktree; the branch is the deliverable; diff for review | asset directory with numbered versions and a provenance manifest (model, prompt, seed, inputs per file); the manifest is the deliverable |
| `Verifier` (stop condition + review input) | visible test pass; reviewer reads the diff | critic loop: the reviewer (vision) grades the output against the brief and a style-guide skill, bounded regenerate rounds; `needs_input` for taste calls becomes a first-class stop, not a later version |
| resident models (`ModelHost`) | three LLMs resident under vllm-mlx's budget | tool models load on demand and unload LRU under the same 90 GB budget; the profile drops the coder and reviewer LLMs to make room |

Already in place for this: content parts (§3) carry images and, with `input_audio`, audio; the chief plan with
`depends_on` covers pipelines (transcode → caption → cut); skills/knowledge are brand kits, style guides, LUT
libraries, past projects; the router's closed-set decisions grade and triage media as well as code. Honest
limits on this hardware: image generation, speech both ways, and image/video *understanding* are practical;
video and music *generation* are slow or immature locally; audio understanding needs an omni model
(Qwen3-Omni) rather than Qwen3.8. A media profile starts with photo and audio and treats video as ffmpeg work.

**Jev as a tool for any role.** `/v1/systemone` is a typed, calibrated choice among named options with a
probability per option; nothing about it is router-specific. A builtin tool `decide(question, options,
criteria?) -> {choice, probabilities, confidence}` over the same client lets any role make closed-set calls
(which of four renders best matches the brief; is this finding blocking; which skill applies), logged like
routing decisions and scorable by `task route:eval` with a labelled set. Roles declare it in `tools` like any
other tool; the router keeps its own policy (§6).

Sources for §4a: https://arxiv.org/abs/2404.13076 · https://arxiv.org/abs/2410.21819 ·
https://www.themoonlight.io/en/review/mllm-as-a-judge-exhibits-model-preference-bias · Muse Glimmer:
https://huggingface.co/meta-models/Muse-Glimmer-30B · https://huggingface.co/mlx-community/Muse-Glimmer-30B-4bit ·
https://github.com/waybarrios/vllm-mlx/pull/812 · Qwen3.8-27B: https://huggingface.co/mlx-community/Qwen3.8-27B-4bit
