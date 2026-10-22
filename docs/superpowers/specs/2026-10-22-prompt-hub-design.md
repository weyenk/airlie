# The prompt hub: rules and steering in files, with a contradiction check

Status: approved by Noah on 2026-10-22. Nothing here is built yet.

## Why

On 2026-10-22 Noah asked whether airlie hardcodes a lot of prompts. It does. The role prompts already live in
`prompts/roles/*.md` and are wired through `airlie.yaml`. Everything else a model reads is written as Python
string literals spread over about a dozen modules. That covers the briefs, the rules inside them, and the
nudges sent partway through a run. In total it is roughly 130 long literals and about 34k characters.
`teams/native_team.py` (about 5k) and `story/briefs.py` (about 6k) hold the most.

Three problems come out of that:

1. **Nobody can see everything a role might be told.** To learn what a coder can hear partway through a run,
   you have to grep `native_team.py`, `tools/loop.py`, `story/rounds.py` and `story/layer_checks.py`.
2. **Contradictions go unseen.** The 2026-10-18 "stuck reading" coder (AIR-28, AIR-7) was partly caused by
   contradictory rules. `prompts/roles/coder.md` and a `briefs.py` rule say tests go in a *new* file, while
   the architect's brief named an existing one. PR 57 patched that case in code. Nothing stops the next one.
3. **The reason for each wording lives in Python docstrings.** Many constants carry an incident note, such as
   `FRESH_RUN`'s "AIR-7 layer 02 fix round, 2026-10-18 …". That history is valuable, but it reads as code, and
   a wording change shows up in review as a code change.

Noah's framing: one hub with two kinds of text, **rules** (standing instructions given up front) and
**steering** (what a role is told partway through a run when something goes wrong), plus a check that they do
not contradict each other. It must not be a "service": nothing here needs its own process.

Related work in flight: AIR-30 (carrying cut-off thinking between turns) and an investigation into steps spent
reading. If those land, several steering texts may stop firing. The hub should make it easy to see which
texts still fire and to delete the dead ones.

## Goals

- Every piece of model-facing prose that is a rule or steering lives in its own Markdown file, with its reason
  (incident, date) kept beside it.
- One loader serves rule and steering files. The code asks for text by id and fills in named values.
- A deterministic contradiction check runs in the existing test gate (`pytest`). It fails when two rules, or
  two steering texts, that can reach the same role give conflicting instructions on the same subject, unless
  one is declared to yield to the other.
- A model-assisted audit, `task prompts:audit`, puts each role's full instruction set in front of the reviewer
  model and lists likely contradictions of any kind, steering against rules included. It is advisory and
  stays out of the gate.
- Every steering text sent during a run is recorded as an event with its id. This shows how often each nudge
  fires, and which ones never do.
- No change in behaviour. A run after the move sends models byte-identical text, except where this document
  says otherwise.

## Non-goals

- **Output-format lines stay in the Python, next to their parsers.** This covers the JSON contracts and reply
  formats: `STORY_CONTRACT` and `LAYERS_CONTRACT` in `story/briefs.py`, the verdict JSON in
  `topology/review.py`, the plan and synthesis JSON in `topology/studio.py`, and the `WINNER:` line in
  `teams/aggregate.py`. A format and the code that parses it change together. Moving one away from the other
  makes that harder, not easier. *(Claude's call.)*
- **Tool descriptions and CLI or config help text** are not prompts in this sense. They stay where they are.
- **No templating engine.** No Jinja: conditionals and assembly stay in Python. A file holds a block of text
  with `{name}` placeholders in `str.format` style, as the constants use today. A brief like `layer_plan`
  stays a Python function that stitches fixed blocks, read from the hub, together with computed parts.
- **No per-model or per-deployment variants of rules or steering**, and no override directory. They ship with
  the code and are reviewed like code.
- **Supervisor advice does not go through the hub in this change.** That advice is generated at runtime by
  Muse, not fixed text. The event the hub records (see *Steering events*) leaves room to log supervisor
  advice in the same stream later. *(Claude's call. Noah has not decided this.)*
- **No runtime check of a rule against the architect's brief.** The 2026-10-18 case was a rule against a
  brief written during a run. The static check here only catches a rule against a rule, or a rule against
  steering. Checking a rule against a live brief is a possible later layer, and the supervisor may be the
  better place for it. *(Claude's call.)*

## Anti-goals

- **A model ends up reading different text by accident.** The move must be verifiable as a no-op for the
  text models receive.
- **The hub becomes a place where logic hides.** If a file needs an `if`, that part belongs in Python.
- **A resumed run stops recognising the end-of-budget messages saved in its history.** Today
  `_without_stale_budget` in `native_team.py` does this by regex. If it breaks, the 2026-10-18 failure
  returns: a coder reading "you have used all 40 steps" in a resumed conversation.

## The shape

### Layout

```
prompts/roles/                 role system prompts: unchanged, chosen per role in airlie.yaml
src/airlie/prompts/rules/      standing instructions placed in a brief or goal message
src/airlie/prompts/steering/   text sent partway through a run on a condition
```

One file per text. The file name is its id, for example `steering/write-now.md` has the id `write-now`. Ids
are unique across `rules/` and `steering/`.

**Why rules and steering ship inside the package instead of next to `prompts/roles/`:** *(Claude's call.)*
They are bound to the code that sends them. Their placeholders must match the call site, and their meaning
depends on the condition the code checks. That makes them part of airlie's code, not deployment config.
The code that sends them (`story/briefs.py`, `story/layer_checks.py`, `story/rounds.py`, `tools/loop.py`,
`teams/native_team.py`) never sees the config, and some of it runs at import time
(`native_team._STALE_BUDGET`). Tests call it with no config, and about 177 tests build temporary configs that
have no prompt folders. A config can also live outside the repository (`AIRLIE_CONFIG`). So these files load
from the package with `importlib.resources`, once, cached, with no config involved. Role prompts are
different: `airlie.yaml` chooses them per role, so they stay where they are.

### File format

Each file is Markdown with front matter: the same `---` block `skills/library.split_frontmatter` already
parses. Its fields:

| field | kinds | meaning |
|---|---|---|
| `kind` | both | `rule` or `steering`; must match the folder |
| `roles` | both | which roles can receive it (`coder`, `tester`, `architect`, …, or `any`) |
| `fields` | both | the placeholders the body uses; the loader checks the set matches exactly |
| `when` | steering | one sentence, for people: the condition that sends it (for example "two-fifths of the steps used and nothing written") |
| `governs` | both | the subjects it gives an instruction on, each with a value; see *Contradiction check* |
| `yields_to` | both | ids it explicitly gives way to when they disagree |
| `why` | both | the incident history: what went wrong, the run or issue id, the date. The docstring notes move here |

The body is the exact text sent, with `{name}` placeholders.

`split_frontmatter` today handles only flat `key: value` lines. Fields like `roles`, `governs` and `yields_to`
need lists and mappings. The builder can either extend the parser or read the front matter as YAML; PyYAML is
already a dependency through config loading. Either way, it must not change what role prompts with a flat
`summary:` line return.

### Loader

A new small module, `src/airlie/prompts/`. *(Claude's call on the name and place.)*

- It loads every file under the package's `rules/` and `steering/` once, on first use, and caches them. A
  unit test in the gate loads them all. It fails with a clear message on a duplicate id, a `kind` and folder
  mismatch, or a body whose placeholders differ from `fields`. So a broken file fails `pytest`, not a run.
- `text(id, **fields) -> str` returns the filled body and raises on an unknown id or a missing or extra field.
- Module-level constants that import-time code needs, such as `_STALE_BUDGET`, may read from the hub at
  import. The package files are always present, so nothing needs a fallback.
- Role prompts go through the same loader for their body. This fixes a bug found while planning:
  `orchestrator.py:209` and `topology/diversify.py:40` send `role.prompt.read_text()`, *including* the front
  matter, as the system message, while `studio.py` and `review.py` strip it with `prompt_text`. After the
  move, every path strips it. This is the one intended change to what models receive. Call it out in the PR.
- Role prompt paths still resolve from `airlie.yaml`, as today. They still fail config validation when the
  file is missing.

### What moves

These are the starting inventory; the builder confirms the full list with the same AST sweep used to size this
(long, non-docstring string literals under `src/`).

- **Steering** (`prompts/steering/`):
  - from `teams/native_team.py`: `MALFORMED`, `NO_TOOLS`, `LAST_STEP`, `WRITE_NOW`, `WRITE_ONLY`,
    `TESTS_NOW`, `CODE_NOW`, `CHANGE_NOW`, `REPEATED`, `CYCLED`, `REPEAT_WRITE_ONLY`, `ACT_FIRST`,
    `STILL_RED`, `LAST_TURN_RETRY`, `ONE_STEP_LEFT`, `FRESH_RUN`, `READ_REFUSED`, `READ_LEFT`;
  - the out-of-tool-calls text in `tools/loop.py`;
  - from `story/rounds.py`: `ROUND_NUDGE`, `FIX_NUDGE`, `UNFINISHED_NUDGE`;
  - from `story/layer_checks.py`: `AMEND` and the fixed parts of `feedback`, `amend_feedback` and
    `LOOSE_MATCH`;
  - from `topology/diversify.py`: the branch-continuation text.
- **Stays in Python:** `teams/protocol.cut_off_text`. It is what a *person* reads when a reply hit
  `max_tokens`: it is raised as an `EngineError` or becomes `final_text`, and it branches on
  `thinking_budget`. It is not steering.
- **Rules** (`prompts/rules/`):
  - from `story/briefs.py`: `TEST_RULES`, `PRE_CODE_CHECK`, `STATIC_CHECK`, `TESTS_ONLY`, `CONTRACT`, and the
    fixed paragraphs inside `refine_brief`, `survey_brief`, `layers_brief` and `layer_plan`;
  - `REFINE_FIELD` from `story/rounds.py`;
  - the goal-message text in `native_team._goal_message`;
  - the "you already worked on this subtask" text in `topology/studio.py`;
  - the client preamble in `serve/app.py`;
  - the summarizer and judge instructions in `lts/`, if they are prose rather than formats.
- **Short labels inside the finding lists** (`PASSES_BEFORE_CODE`, `ASSERTION_FREE`, `NO_NEW_TEST`,
  `EXISTING_FAILS` in `layer_checks.py`): these are tested as constants and appear inside generated feedback.
  The builder decides whether they move, as rules or as one file with several entries. Keeping them inline is
  acceptable. *(Left to the builder.)*

Python names that tests import today, such as `LAST_STEP`, `REPEATED`, `MALFORMED` and `REPEAT_WRITE_ONLY`
from `native_team`, may stay as module-level names bound from the hub. Alternatively, the tests can switch to
`text(id, …)`. *(Left to the builder.)*

### Resumed runs and stale budget messages

`_without_stale_budget` recognises the last run's end-of-budget messages by regex, built from the constants'
text. After the move, the regexes are built from the hub's text for the same ids. Behaviour stays the same as
today, including today's weakness: if a wording changes, conversations saved under the old wording are no
longer recognised.

The builder may also tag hub-sent messages with their id and persist it through `dump_messages` and
`load_messages`, so recognition goes by id. That would need a new optional field on `engine.types.Message`
that never reaches the wire. It is worth doing only if it stays small, and it must keep the regex path for
conversations saved before the change. *(Left to the builder. The minimum is regexes from the hub.)*

### Steering events

Every time the code sends a steering text, it emits one event, `steer`, with the id, the role, the team or
layer, and the step, on the existing event stream (`obs/events.py` or the story state's `emit`, whichever that
code path already has). The metrics tab or a later task can then count how often each id fires. Adding a view
for this is not part of this change. *(Claude's call: the event name and fields are left to the builder.)*

### Contradiction check (deterministic, in the gate)

Each rule and steering file lists, in `governs`, the subjects it instructs on and the stance it takes. For
example:

```yaml
governs:
  tests.location: new-file
  reading: stop
```

**Precedence:** *(Claude's call.)* Steering outranks standing rules by default. That is its job: "reading
is switched off" exists to override a role prompt that says to read first. So the check does not flag steering
against a rule. It does flag:

- **rule against rule**, which includes role prompts against rules, and role prompts against each other where
  they share a role. This is the 2026-10-18 class: `coder.md` and a `briefs.py` rule against the coder's
  other instructions;
- **steering against steering** for the same role and subject. Two nudges that can both fire in one run must
  not tell the model opposite things, for example "stop reading" and "read the file first".

The check:

1. For each role, it collects the texts that can reach it:
   - the role prompt, which `airlie.yaml` assigns to that role;
   - every rule and steering text whose `roles` includes that role or `any`. The hand-written `roles` list is
     trusted. Nothing is worked out from `airlie.yaml` tool lists or flags.
2. Within rules (role prompts included), and separately within steering: for each `governs` subject where two
   texts reaching the same role give different values and neither lists the other in `yields_to`, it fails.
   It names both files, the subject and both values.
3. The role prompts in `prompts/roles/` also get a `governs` front-matter field. A role prompt is a source of
   rules too: `coder.md` says "tests in a new file under `tests/`". Their existing flat `summary:` line keeps
   working. The check reads the role prompts of the repository's own `airlie.yaml`.

`yields_to` is for the deliberate exceptions inside a kind. For example, the rule "tests in a new file" yields
to the rule "the brief names the test file" (PR 57's precedence, now written down).

Subjects are free-form dotted names, with no central registry. A test lists every subject in use and every
place it appears, so a near-duplicate (`tests.location` vs `test.location`) is visible in review. *(Claude's
call. A registry was rejected as ceremony for about 30 files.)*

This check is only as good as the `governs` tags. It catches a contradiction once someone has written down
what each text instructs. Writing the tags for the first time is part of the move, and it is the useful part:
it forces each text's instruction to be stated in a word or two.

### Model-assisted audit (advisory, not in the gate)

`task prompts:audit` puts together, for each role, the text it can receive: role prompt, rules and steering
bodies, with placeholders left as `{name}`. It asks the reviewer model (Muse Glimmer, the `reviewer` role's
model, through the managed engine) to list instructions that contradict each other, quoting both. It writes a
report under `runs/` and prints a summary. It needs the engine running, is non-deterministic, and never fails
anything. Its job is to find contradictions the `governs` tags miss, which then become tags.

The audit's own instruction is a rule file too (`prompts/rules/audit.md`). Its output format stays inline in
the task's code, by the non-goal above.

## Constraints

- Python 3.12, uv, Taskfile for every action (`task prompts:audit` is a new task). It must pass the test gate
  in `AGENTS.md`.
- No new dependency unless the builder finds PyYAML insufficient for front matter. It is already used for
  config.
- Byte-identical text: add a test that renders every moved text with sample fields and compares it to the old
  constant's output. Capture the old outputs before the move, as fixtures generated once from the current
  code. Delete that test after one release, or keep it as a snapshot. *(Left to the builder.)*
- Keep the repository's comment style: the incident and date notes move into `why`, not lost.
- The move can land as several PRs, one module group at a time, behind the same loader. *(Left to the
  builder: order and split.)*

## Left to the builder

- Module name and API details beyond `text(id, **fields)`.
- Whether the YAML front matter extends `split_frontmatter` or replaces it for these folders.
- Whether tests keep importing module constants.
- Message tagging for stale-budget recognition (optional, see above).
- Event name and fields for `steer`.
- Whether the short finding labels in `layer_checks.py` move.
- PR split and order.
