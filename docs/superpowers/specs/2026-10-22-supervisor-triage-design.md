# The supervisor: Muse helps the tester and coder before a layer blocks (layer 1, the trial)

Status: draft for Noah's review, 2026-10-22. Nothing here is built.

## Why

On 2026-10-21, AIR-9 layer 02 blocked three times. Each time a person, Noah or Claude, read the traces, found
the real cause, and got the run moving again:

1. **The tester's test used `Path` and `LocalBoard` without importing them.** Its one retry got the generic
   pre-code feedback (`layer_checks.feedback`). The tester spent the whole retry re-arguing the design, with its
   thinking cut at its 4096-token budget on every step. It never edited, then sent an empty reply that counted
   as done. PR #118 now nudges an empty reply with `ACT_FIRST`. The deeper problem remains: the retry carried
   no diagnosis. One line, "add the two imports and edit now", would very likely have saved it.
2. **The engine was down** (ConnectError), but the block read "no new test". A parallel session is fixing this
   deterministically.
3. **An existing test, `test_the_mcp_server_calls_only_routes_serve_has`, opened the real
   `~/.local/state/airlie/auth.sqlite`.** The check's sandbox refused it, and the tester was blamed. The
   tester's retry could not fix a test that was not its own. A parallel session is fixing this
   deterministically too: the base tree runs under the sandbox.

Noah asked whether airlie could do this triage itself. He wants:

- **The reviewer model to watch the tester/coder loop and offer help when a run is in trouble.** That's Muse
  Glimmer, a different family from the Qwen roles it helps.
- **A record of every time it had to step in.** A problem that keeps coming back should eventually be fixed in
  airlie itself, through a change Noah merges.

He chose to do this **in layers**. This spec is layer 1 only.

## The layers

| Layer | What | When |
|---|---|---|
| **1 (this spec)** | Muse helps inside the run, at the moments the pipeline already treats as trouble. It is advisory only, and every intervention and its outcome is recorded in a ledger. | Now: a trial |
| 2 | Muse moves to its own managed vllm-mlx engine (`supervisor`, for example port 8082), taken out of `main`'s model list so it is loaded once. It stays up when `main` restarts, so it can watch `main` and recover it. Open for that spec: whether airlie restarts `main` on Muse's word, or Muse asks on the card. | After the trial shows results |
| 3 | A category that recurs (for example three resolved interventions) becomes a card on airlie's own board. Muse writes up the pattern, the runs it happened in, and the permanent fix it proposes (a prompt, a check or a test fixture). The story pipeline builds that card into draft PRs: Qwen builds, Muse reviews, and only Noah merges. Later occurrences link to the open card rather than filing duplicates. | After layer 2 |

Layers 2 and 3 are recorded here so layer 1's ledger is shaped to serve them. Each gets its own spec.

## Goals (layer 1)

- At each trouble point, Muse reads the evidence and works out what is actually tripping the role. It then
  writes the message that role gets, in place of today's generic text.
- A layer that still blocks carries Muse's diagnosis on the card, not just the raw finding.
- Every intervention, its outcome and Muse's judgement go into an append-only SQLite ledger: the audit trail
  of fixes outside the normal pipeline.
- The trial is readable. The ledger alone shows how often Muse's message was followed by the role
  succeeding, and where Muse had a fix but no attempt left to use it.

## Non-goals (layer 1)

- **No extra attempts.** The budget stays as it is today: one pre-code retry, one static-check retry, one gate
  follow-up, one test mend. Noah's decision on 2026-10-22, for two reasons:
  - In AIR-9 the retry was wasted by bad feedback, not missing attempts.
  - With the budget fixed, any drop in blocks is Muse's diagnosis and nothing else.
- **No change to who gets sent back.** The pipeline still decides deterministically which role a retry goes
  to. Muse records which role it *would* have sent it to (`would_route_to`), as evidence for a later layer.
- **No skipping a retry, and no early block.** Muse only writes messages.
- **No engine recovery** (layer 2), and **no fix cards** (layer 3).
- **Not the chief.** The chief is the generalist, the family that gets stuck. Noah chose the reviewer model.

## Anti-goals (a failure even if it works)

- **Muse loosens a check, or clears a finding.** It never edits code or tests, never marks a finding as not
  applying, and never changes what the checks or the gate accept.
- **Muse hides ground truth or drops a rule.** The message each trigger sends today stays whole: the
  findings and the rule paragraph (`feedback`'s "every test you wrote must fail now… do not write the feature",
  `AMEND`, `GATE_FAILED`, `REPEATED`/`CYCLED`). Muse's diagnosis and next action are **added** as a
  "Supervisor:" section between the findings and the rules. Nothing is cut, so no rule depends on Muse
  restating it. *(Claude's call, from the anti-goal: the builder check showed those paragraphs hold hard
  rules, not just advice.)*
- **A broken supervisor stops a run.** If Muse errors, times out or returns something unusable, the role gets
  today's text and the run goes on. An event records the fallback.
- **Supervisor turns crowd out the work.** Muse fires only at the trouble points below, at most once each, and
  under a per-layer cap.

## Where it fires (the trouble points)

Each point already exists. Muse supplies the text sent there.

| Trigger | Where today | What the role gets today | With the supervisor |
|---|---|---|---|
| `precode_retry` | `StoryPipeline._check_tests`, before `orch.follow_up("tests", feedback(found))` | `feedback(found)` | Today's text, with Muse's section added |
| `static_retry` | `StoryPipeline._check_static`, before `orch.follow_up("build", feedback(found, before_code=False))` | `feedback(..., before_code=False)` | The same, for the coder |
| `gate_retry` | `StudioOrchestrator`'s gate follow-up (`gate_follow_up="build"`) | `GATE_FAILED` with the gate's tail | Today's text, with Muse's section added |
| `amend` | `StoryPipeline._amend_tests`, before `orch.follow_up("tests", amend_feedback(...))` | `amend_feedback(failing, account, review)` | Today's text (failing list, coder's account, reviewer's points, `AMEND`), with Muse's section added |
| `repeat_guard` | `native_team`, where `REPEATED` or `CYCLED` is first sent (a `nudge_stuck` role) | `REPEATED` / `CYCLED` | `REPEATED`/`CYCLED` with Muse's section added, sent once as today. `REPEAT_WRITE_ONLY` and the run-ending third streak are unchanged. |
| `block` | `StoryPipeline._build`, where `StoryBlocked` is raised for a layer | the block reason | The block reason, then "Supervisor:" and Muse's diagnosis in the card note. Diagnosis only: no attempt is added. |

`ACT_FIRST`, `STILL_RED`, `LAST_TURN_RETRY` and `ONE_STEP_LEFT` keep their fixed text. They fire often, and
fixed text works for them.

`repeat_guard` is the only trigger inside a running team. It needs a hook on the team context: an optional
async callable that returns Muse's section, or `None` for the fixed text alone. That trigger never stops the
run.

**Story runs only.** The supervisor fires only when the story pipeline runs a layer. Every ledger row has a
story key and a layer. In any other studio run (`task run --topology studio`) the hook is `None` and nothing
changes. *(Claude's call.)*

## What Muse sees

Built by plain code into one message, each part capped:

- **The trigger and the findings**, as `Finding` records (`kind`, `test`, `why`, `source`), or the gate's
  failing tail.
- **The layer**: its brief and acceptance criteria (as `layer_plan` gives them), and the role's brief.
- **The role's recent work**, from the trace (`trace/<subtask>/[round-N/]step-NNN.json`): for the last few
  steps, `reasoning` (its tail), `assistant_text` and the tool calls with a short observation. For
  `repeat_guard`, the team's in-memory messages instead. This is how Muse sees, for example, a design argued
  again and again with thinking cut at the budget.
- **The diff** of the layer's worktree against its base (capped, as the review diff is).
- **This layer's story events** from `events.jsonl`, and any earlier interventions on this layer, so a second
  triage knows what the first said.
- **Recall**: a few past interventions with the same trigger and finding kind whose outcome was `helped`, with
  their category and message. This is "it gets faster at what it has seen".

## Muse's tools (read-only)

- `read_file`, `list_dir`, `grep` on the layer's worktree.
- `run_test`: one pytest node id on the layer's worktree, run as the pre-code check runs it (same confinement,
  same timeout, no bytecode written).

Muse may make a handful of calls (a small `max_steps`), then must answer. It has no write tools and no engine
or board tools.

## What Muse answers

One JSON object:

```json
{
  "category": "missing_import",
  "owner": "tester",
  "diagnosis": "The new test uses Path and LocalBoard without importing either; it errors at collection, not at the assertion.",
  "evidence": ["run_test tests/test_x.py::test_y -> NameError: name 'Path' is not defined"],
  "message": "Your test fails for the wrong reason: Path and LocalBoard are not imported...",
  "would_route_to": "tester",
  "fix_if_attempt": null
}
```

- **`category`**: a short snake_case name. Muse picks from a seeded list, or adds a new one (`other_*` is
  not allowed). The seed list comes from the cases seen so far:
  - `missing_import`
  - `sandbox_escape` (a test reaches real state outside the worktree)
  - `design_relitigation` (re-arguing settled design, often with thinking cut at its budget)
  - `empty_reply`
  - `repeat_loop`
  - `wrong_acceptance_test` (the test asserts something the brief does not say)
  - `not_this_roles_test` (the failing test is not one the role wrote)
  - `environment`

  Layer 3 counts patterns by this name, so a stable name matters more than a perfect one.
- **`owner`**: one of `tester`, `coder`, `existing_code`, `environment`, `unknown`.
- **`message`**: the section added to what the role gets. Required unless the trigger is `block`. It must be concrete: what
  failed, why, and the one next action.
- **`would_route_to`**: which role Muse thinks should get this retry. It is recorded only.
- **`fix_if_attempt`**: at `block`, the message Muse would send if it had one more attempt, or `null`. This
  is the trial's evidence for or against adding an attempt later.

An answer that does not parse, or lacks a required field, is a fallback, not a retry of Muse.

## The ledger: `~/.local/state/airlie/supervisor.sqlite`

The ledger is its own file, apart from `board.sqlite` (the board has been reset before, and the audit trail
must survive that). Like `board/store.py`, it uses stdlib sqlite3 in WAL mode, with one connection per store
and a lock around writes.

It is **append-only**: no row is updated or deleted. An outcome or a verdict is a new row that points at the
intervention.

- **`interventions`**: id, at, story key, layer, studio run id, subtask, role, trigger, finding kinds,
  category, owner, diagnosis, evidence (JSON), message sent (or `null` at a block), `would_route_to`,
  `fix_if_attempt`, model, seconds, tokens, and `fallback` (the reason, when Muse's answer was not used; the
  role then got today's text).
- **`outcomes`**: intervention id, at, outcome, detail. The outcome is written by plain code when the next
  thing the trigger leads to is known:
  - `precode_retry`: `helped` if the next pre-code check passes, else `not_helped`.
  - `static_retry`: the same, for the static check.
  - `gate_retry`: `helped` if the gate passes after the follow-up.
  - `amend`: `helped` if the mend is kept (`tests_mended`).
  - `repeat_guard`: `helped` if the role's run ends ok without reaching `REPEAT_WRITE_ONLY`.
  - `block`: `blocked` (nothing to judge automatically).
  - A run that dies first: `unknown`.
- **`verdicts`**: intervention id, at, verdict (`right` or `wrong`), note. A person's judgement of Muse's
  diagnosis, mainly for blocks.

The story's `events.jsonl` gets a short `supervisor_triage` event (trigger, category, owner, ledger id) or a
`supervisor_fallback` event (trigger, reason), so anyone reading a trace sees Muse stepped in. The full detail
lives in the ledger.

Layer 3's `patterns` table (category, count, filed card, card state) is not built now. In layer 1 it is a
query over `interventions`.

### Reading it

Three tasks, each a thin CLI over the store (names are the builder's to adjust to the Taskfile's style):

- `task supervisor:log`: recent interventions, one line each: when, story/layer, trigger, category, outcome.
- `task supervisor:stats`, which answers the trial's questions:
  - the help rate by trigger and by category
  - the fallback rate
  - the blocks where `fix_if_attempt` was given
  - the categories by count
- `task supervisor:verdict -- <id> right|wrong [note]`: records a person's verdict.

A web view, such as the Metrics tab, is later work.

## How it's configured and run

- **A new role, `supervisor`**, in `airlie.yaml`: `prompt: prompts/roles/supervisor.md`, `model: reviewer`,
  with the read-only tools above. It has a small `max_steps` and modest thinking. Per the 2026-10-19 lesson,
  `max_tokens` must clear `thinking_budget`. The reviewer seat is unchanged; this is a second role on the same
  model.
- **`story.supervisor: { enabled: true, per_layer: 6, timeout_s: ... }`**:
  - `enabled`: on by default for the trial. Off gives today's behaviour exactly, for comparison runs.
  - `per_layer`: caps Muse's turns per layer. Past it, the fixed text is used and a fallback is recorded.
  - `timeout_s`: bounds one triage. Past it, the run falls back to the fixed text.
- **Muse runs on `main`, where it already is.** In the studio profile `reviewer` is preloaded next to the
  generalist, so there's no model swap. If `main` is down, Muse is too, and the run falls back. Layer 2 fixes
  that.

## Worked examples (AIR-9 layer 02)

Two kinds of check, both Claude's call:

- **In the test gate (CI): plumbing, with a scripted Muse answer.** Fixtures are **synthetic**, modelled on
  these cases. The repo is public, and the real traces hold model reasoning and local paths, so no excerpts
  of them are committed. The tests cover:
  - Muse's section reaches the role, with the rules still there.
  - The ledger and outcome rows are written.
  - The fallback works.
  - The `block` note carries the diagnosis.
- **Once, before the trial is called built: the real Muse on the real case** (test as the user). Rebuild AIR-9
  layer 02's worktree at the tester's commit from runs `20261021-132205-8733` and `20261021-144830-92ca`,
  run the real triage on case 1, and record its answer and whether it names `missing_import`. That result
  goes in the PR. Without it the trial starts blind.

The cases:

1. **Missing imports.** At `precode_retry`, Muse sees `NameError` in the finding, or confirms it with
   `run_test`.
   - It answers `missing_import`, owner `tester`, with a message naming both imports and saying to edit now.
   - The ledger gets an intervention row, then an outcome row from the next check.
2. **Engine down.** Muse cannot be reached either.
   - It falls back, a `supervisor_fallback` event is written, and the role gets today's text.
   - The parallel fix reports the real cause.
3. **The existing test reaching `auth.sqlite`.** With the parallel attribution fix in place, this should no
   longer be put on the tester. If a variant still is, Muse answers `not_this_roles_test` or `sandbox_escape`,
   owner `existing_code`.
   - Its message tells the tester which test is not theirs and to leave it alone, and what (if anything) of
     theirs to fix.
   - If the layer then blocks, the card carries that diagnosis.

## How we'll know it works

From the ledger, after a few stories:

- **The rescue rate**: `helped` outcomes over interventions, by trigger. Compare it with the retry success
  rate before the trial, from older `events.jsonl` (`test_checks_retry` followed by `test_checks_passed`).
- **Hand triage**: the blocks Noah or Claude still had to open by hand should fall.
- **The extra-attempt case**: how often a block had a `fix_if_attempt`. If it's often, that is the case for
  an extra attempt in a later layer, and the ledger shows exactly where.
- **Diagnosis quality**: `right`/`wrong` verdicts on blocks.

## Constraints

- Python 3.12 with uv; every action is a Taskfile task; the AGENTS.md test gate passes.
- One Mac: supervisor turns share `main` with the roles they help.
- Only a human merges. Nothing in this layer writes to a repository or the board, beyond the block note the
  pipeline already posts.
- Model output is data. Muse's answer is parsed, never executed, and its `message` reaches only the role's
  conversation.

## Left to the builder

- The module layout. A small `airlie/supervisor/` package (store, triage, prompt building) is the obvious
  shape.
- The exact caps on each input part and the trace tail length, and `max_steps`, `timeout_s` and the
  sampling for the `supervisor` role.
- How the `repeat_guard` hook is threaded through `TeamContext`, and how the triage call avoids holding the
  studio's write lock or semaphore while Muse thinks.
- The prompt text in `prompts/roles/supervisor.md`, within the rules above: concrete, one next action, never
  telling a role to weaken a test.
- Where outcomes are written. Each trigger's "what happened next" is already an event or a return value in
  the pipeline and studio.
