# The story pipeline screen matches the pipeline

2026-11-02. Mockups: [2026-11-02-pipeline-matches-diagram-mockups.html](2026-11-02-pipeline-matches-diagram-mockups.html)
(published at https://claude.ai/artifact/N9YXEnYkcMHiRv8HbpLQsW). Noah picked **option C**.

## Why

Noah keeps seeing a story that is clearly working while the workflow screen (`/workflows/<key>`) looks idle.
The pipeline writes every step to the story's `events.jsonl` and its studio runs' `events.jsonl`, but
`src/airlie/serve/flow.py` reads only a fixed handful of event kinds, so whole steps have no box.

Seen on 2026-11-02:

- **AIR-47, 15:16 to now.** The build lane's "draft all packages" conversation ran the architect's `packages`
  ask and two `critic` asks and logged `package_drafted` for L01 and L02. The screen showed five pending
  layers and nothing running, for over an hour.
- **AIR-48 layer 02, run `20261101-140822-8efc`.** The coder's `subtask_done` lands before the gate runs.
  The gate then failed (`gate_failed`) and the coder worked a whole `follow_up`, while the Build box
  already read finished.

The goal is debugging: at any moment, the lit box says which step of the pipeline is running, and the
boxes, read left to right, say what happened. Noah's ASCII diagram of the pipeline (the plan lane, then the
build lane, then the exits) is the reference for which steps exist.

## Goals

1. Every step in the diagram has a box, or a row in a box's card, and it is lit while that step runs.
2. A "now" line on the pipeline panel names the deepest running step.
3. Layout stays as simple as today's row (option C): plain boxes, the detail in a hover card.
4. On a phone: one list, finished parts folded, the active step open, and a bottom sheet on tap.
5. Stories recorded before this change still draw, showing what their events hold.

## Non-goals

- **No stall warning.** The mockup's amber "quiet for N min" bar is out. Noah wants to wait for a system
  stable enough to benchmark what "too quiet" means.
- No new box types beyond the six in the mockups.
- No change to what the pipeline *does*. The only pipeline changes are extra event fields or events that
  the screen needs (see "Events the pipeline adds").
- The run flow screen (`/runs/<id>`, `RunWorkflowScreen.vue`) and the epic and initiative screens are left
  as they are.

## Anti-goals

- A box that reads finished, or a screen with nothing lit, while the story's last event says something is
  running. That is the bug this fixes.
- A screen so busy it is harder to follow than today. Noah picked C over B for being simple.
- The screen guessing. Where an event lacks the field the screen needs, the pipeline adds the field; the
  screen does not infer it from order (except as a fallback for stories logged before the field existed).

## What it looks like (laptop: option C)

The Pipeline panel keeps today's left-to-right row (`Pipeline.vue`, desktop branch) and gains:

**The now bar** across the top of the panel, under its header. It shows the path to the deepest running
step, like `Draft packages › L03 › package · architect`, followed by that step's step count and elapsed time and
`last event Ns ago` as plain muted text. It has no color change and no threshold (see the non-goals). When
nothing is running it says where the story stands instead: `Planned · waiting for the build lane`,
`Blocked at L02 build 2 of 2`, `In review · 5 PRs`, and so on. It always shows.

**The row, in order:**

```
intake → base → gate → [Survey | Refine] → Plan layers → Three amigos → ◆ PLANNED
  → Draft packages → [layer lanes] → publish → review → [exit row, when there is one]
```

Each layer lane: `Package → checks → place → Build → squash`.

**Box shapes** (status colors unchanged: green passed, blue running, red failed, dashed pending):

| Shape | Used for | Look |
|---|---|---|
| Model step | Survey, Refine, Plan layers, Three amigos, Draft packages, Package, Build, Diagnose | Today's `JobBox` |
| Code step | intake, base, gate, checks, place, squash, publish, review, fix card | Square corners, monospace label, a small square mark that takes the status color |
| At the same time | Survey and Refine | A bracket around the stacked boxes, labeled "same time" |
| Milestone | PLANNED | A diamond with the time `story_planned` was logged |
| Rounds and turns | Plan layers rounds and fixes, amigos turns, draft turns | Squares, in the hover card only |
| Steps inside a box | Build (coder → done-check → reviewer → gate → follow-up), Package (draft → critic → proof) | A list in the hover card only |

In C the last two appear only in hover cards. The boxes stay plain.

**The box's second line names the live inner step** while the box runs, like `coder · gate follow-up` or
`L03 · package`, and otherwise shows role and time as today. That line is what stops a box looking idle.

**Warn tags on the box itself** only where something needs attention: an attempt count over 1 (`2x`,
today's), `build 2 of 2`, `re-planned`, and the budget's `over budget` (the `budget_exceeded` event).

**Hover cards.** Hovering or focusing a box that has a card opens a small card beside it (the mockup's
`#hpop` behaviour: fixed position, kept on screen, hidden on scroll and on leave). The cards' content is in
the mockups' "hover cards, laid open" block:

- gate: the commands as read, the AGENTS.md commit.
- Plan layers: round squares (passed, unusable, not run), fix and retry squares, the result, and who ran it
  for how long.
- Three amigos: turn squares (a dot for airlie's checker turns), each role's last vote on requirements,
  changes and proof, objections raised and resolved, how it ended (agreed, or owners decided with the
  open objections).
- Draft packages: one line per layer turn (package → critic verdict → revision), with times and the run.
- Package: draft taken or asked, the critic's verdict, any proof revision (tester) or criteria revision
  (product), and the try count.
- Build: coder, done-check, reviewer (fix rounds), gate (red tests), gate follow-up, each with status and
  time, plus the supervisor's triages for this build.
- Survey, Refine, Diagnose: role, steps, time, and the run.

Clicking a box still opens `JobDetail` below the panel, as today. Cards are a summary; the detail is the
trace.

**Layer lanes.** A layer that has not started shows its header line only (`L04 · single-pr-state-bench`
plus a `drafted` or `drafting` tag), as in the mockups' option A and C rows. A layer that has started shows
its job row. A layer built more than once gets one job row per build, labeled `build 1`, `build 2`, with
`↩ re-planned` ending each failed one (the mockups' AIR-48 close-up).

**Exit row.** A story that stopped shows one more row at the end of the pipeline:

- `BLOCKED` at <where> → Diagnose (Muse; model box) → fix card (filed, linked or noted, with its key) →
  card → blocked.
- `WAITING` on <key>: its reason.
- `CANCELLED` during <step>.

The red `blocked` reason in the story screen's header (`WorkflowScreen.vue`) stays: it is the first thing read.
The exit row adds the diagnosis, the fix card and the card's move. It renders `story_flow`'s `diagnosis` field,
which no `.vue` file renders today.

## What it looks like (phone)

The phone branch of `Pipeline.vue` becomes the list in the mockups' "On a phone" section:

- The now bar is pinned at the top of the panel while the list scrolls.
- One row per step in pipeline order, under `PLAN LANE` and `BUILD LANE` labels.
- **Finished parts fold.** A finished lane folds to one row with a strip of small chips (one per step),
  for example `Planned · 1h 03m`. A finished layer is one row (`L01 built · 1 commit · 38m`). A failed
  earlier build of a layer is one row (`build 1 · red → re-planned`). Tapping a folded row opens it.
- **The active step is open.** The running stage or layer is expanded with its inner steps listed under it
  (Draft packages' layer turns; a layer's Package, checks and place, Build and its five inner steps). The
  running inner step is outlined.
- **Tapping any row opens a bottom sheet** with that step's hover card at the top and the job detail
  (steps, reasoning, prompt) under it. Noah liked this one specifically.
- The exit row is the last row.

This replaces the phone layout added in #211.

## Where each step comes from

The flow builder reads these. Events are in `stories/<key>/events.jsonl` unless marked *run*, meaning
`runs/<studio_run>/events.jsonl` of the run named.

| Step | Running when | Finished when | Notes |
|---|---|---|---|
| intake | `story_start` with stage `new` | `stage: intake` | |
| base | after intake, no base yet | `state.base` set; `stacked_base` for a dependent story | Shows ref and short sha |
| gate | — (instant) | `gate` event (or `state.gate` present) | Failed: `blocked` whose reason is the missing-gate text |
| Survey | `run` stage `survey` | `survey_done` / `survey_failed` | |
| Refine | `run` stage `refine` | `stage: refine`; `refine_skipped` | Retries: `retry` stage `refine` |
| Plan layers | `run` stage `layers` | `plan_done` | Rounds: `plan_round`, `plan_round_unusable`; fixes: `plan_fix`, `retry` stage `layers`; over budget: `budget_exceeded` stage `plan` |
| (skill pick) | `run` stage `skill` | `skill` subtask `amigos` | A line in the amigos card, not a box |
| Three amigos | `amigos_start` (new; old stories: the `skill` event with subtask `amigos`) | `amigos_agreed` / `amigos_owner_decided` | `amigos_turn` is logged when a turn *ends* (it carries `wall_ms`); the running turn is the next role in the round |
| PLANNED | — | `story_planned` | |
| Draft packages | `packages_drafting` | every layer has a draft, or drafting stopped | One `run` stage `packages` for the whole conversation (its later turns log no `run`). Turns: `package_draft_start` (new, layer), `run` stage `critic` (layer, new field), `amigos_critic` (layer), `package_drafted` (layer); stops on an `unusable` or `proof_change` draft (`state.drafts`) |
| Package (per build) | `package_start` (layer) | `package_ready` / out of tries | Critic: `amigos_critic`; proof: `run` stage `proof` + `amigos_proof_revised`; criteria: `story_criteria_revised`; resumed: `package_resumed` |
| checks | `package_checks_start` (new, layer, attempt) | `package_ready` / `package_returned` | Every existing check event is an outcome logged after the check returns. Kinds as `CHECK_KINDS`, plus the older `vacuous_test`, `unparseable_test`, `test_checks_red_base`; any other unknown kind on a layer counts as a finding, not an error |
| place | `layer_start` | `package_placed` | |
| Build | `layer_start` (studio_run) | `layer_built`, `layer_replanned`, or the run's `run_done` | Inner steps from *run*: `subtask_start`/`step` (coder), `done_check`/`done_check_done` (only some runs log these; no events, no row), `review`/`fix_round` (reviewer), `gate_start`/`gate_passed`/`gate_failed` (new, both gate runs), `follow_up` then the second gate run |
| squash | Build passed, no `layer_built` yet | `layer_built` | |
| publish | `stage: build` | `published` / `stage: publish` | |
| review | — | `stage: review` | `described` adds the PR numbers |
| exit | — | `blocked`, `waiting` (new field `on`: the key it waits for), `cancelled` | Diagnose: `diagnosis_failed`, the diagnosis file (`diagnosis.latest`); fix card: `fix_filed` / `fix_linked` / `fix_noted` / `fix_did_not_take` |
| beside a build | — | — | Supervisor: `supervisor_triage`, `steer`, placed by time (between the build's `layer_start` and its end), not by their `layer` field, which can be stale (AIR-48 logged a `trigger: block` triage for layer 01 while 02 was blocked). Retired mutation events (`mutation_gate*`, `run` stage `mutation_review`) are ignored |

A box is **running** only when its start is logged, its end is not, and the story is live. That rule decides what
the now bar points at: the deepest running box.

**Live** uses the rule `src/airlie/serve/running.py` already has: this server's job for the story is running, or the
story's or its current run's last event is under `ACTIVE_S` (an hour) old. A story that is not live and has no stop
event (a killed process) draws its open box as **stopped**: dashed, with the last event's time. The now bar then
says `Stopped at L02 › Build › coder · no event since 16:03`. That is a fact about a dead process, not the stall
warning Noah deferred: it never shows while the process is alive.

### A story that stopped and resumed

AIR-48 is the real case: three `blocked` events, each followed by a resume, and layer 02's work spread over all
three sessions (package tries with no build after them, build 1 red and re-planned, build 2 red, then more
package tries and a proof revision). `package_start` attempt numbers restart at 1 in each session, and a proof
revision repeats an attempt number (`attempt -= 1` in `_ready_package`). So:

- A layer lane has one row per **try cycle**, in log order. A cycle is a run of package tries that ends in a
  build (`layer_start` to `layer_built` / `layer_replanned` / a stop) or in a stop with no build. Rows are
  labeled by what they reached: `build 1 · red → re-planned`, `build 2 · red`, `package · stopped`.
- A stop between cycles shows as a thin divider in the lane, `blocked · resumed 11-01 14:02`, with its
  diagnosis and fix card in that divider's hover card.
- Tries are numbered by their position in the cycle, not by the event's `attempt` field.
- The exit row shows the latest stop, with a `blocked 3 times` tag when there were more.
- Lanes come from `state.layers`. Events for a layer that is no longer in the state (one dropped by hand) are
  not drawn.

Noah chose this on 2026-11-02: every try cycle stays visible, with a divider at each block and resume.

### Events the pipeline adds

Where a step logs no start, or an event lacks what the screen needs, the pipeline adds it:

1. **`layer` on the `run` event of a layer's ask** (stages `package`, `critic`, `proof`, `criteria`). The `run`
   event is emitted in `Asker.start_ask` (`src/airlie/story/asking.py:162`), which `ask`, `conversation`,
   `ask_in_rounds` and the skill pick all share, so the layer is an optional argument threaded through them.
   Stories without it fall back to pairing each `run` with the next event that names a layer.
2. **`package_draft_start`** (layer) for each turn of the drafting conversation, held or resumed. The
   conversation logs one `run` at its start and none per turn, so this is the only way to light the running
   turn.
3. **`package_checks_start`** (layer, attempt) when `PackageChecker.check` begins. AIR-48 L01 spent 233 s in
   the checks with no event.
4. **`amigos_start`** when the meeting begins.
5. **`gate_start` and `gate_passed`** in the studio run, around *both* gate runs: `_gate` calls `_tests_pass`
   once at first and again after the follow-up (175 s of silence in run `8efc`), so they belong in
   `_tests_pass` (`src/airlie/topology/studio.py`).
6. **`on`** (the card key) on the `waiting` event (`pipeline.py`, `_wait`; the key is in `StoryWaiting`'s text
   today).
7. **A fix:** `package_returned` in `_ready_package`'s StoryError branch logs `studio_run=st.runs[-1]`, which
   can be another layer's build run (AIR-48: L02's attempt 2 names L01's run `20261101-085817-b980`). It
   should name the ask that returned. This corrects a field value, and the Package box reads its tokens from it.

Each is a new field or event kind (or the one corrected field) and changes no behaviour. Older stories draw
without them, with the fallbacks noted in the table.

## The flow API

`GET /v1/airlie/stories/{key}/flow` (`story_flow`) keeps its shape (`nodes`, each with `jobs`) so the other
screens keep working, and grows:

- `now`: `{ path: [labels…], node, job, inner, role, steps, started, last_event }`, or a `state` text when
  nothing runs.
- On a node: `lane: "plan" | "build"`, `shape` per job (`model`, `code`, `milestone`), `group` for the
  survey/refine bracket.
- On a job: `live` (the second line while running), `tags` (warn tags), `card` (the hover card's content
  as plain data: rows of label, squares, text), and for a layer `builds` (one job row per build).
- `exit`: the exit row, or null.

`job_detail` keeps working for every new box, including the code steps (`input_note` "airlie's own check, no
model" as `checks` has today).

## Constraints

- AGENTS.md test gate on every change; `task web:gate` for the web side.
- Vue app conventions as in `web/src/screens/workflow/`: Tailwind tokens from `styles/tokens.css`, both
  themes, the existing `BOX`/`DOT` status styles, `data-*` hooks the tests read.
- The story screen refreshes on the `stories` and `runs` live topics (`liveResource` in
  `WorkflowScreen.vue`); `story_flow` must stay cheap enough to rebuild on every event. Reading each named
  run's `events.jsonl` once per refresh is today's cost and is fine; nothing reads trace step files except
  `job_detail`.
- The hover card works with mouse and keyboard focus. On touch, a tap opens the sheet (no hover).
- `prefers-reduced-motion`: no pulse.

## Decided by Noah

- Option C: A's row, B's detail in hover cards.
- Phone: fold finished, open active, bottom sheet on tap.
- These six box shapes are enough for now.
- No stall warning yet.
- A resumed story shows every try cycle, with a divider at each block and resume.

## Claude's calls (check these)

- The now bar keeps `last event Ns ago` as plain muted text. No color, no threshold. If that already reads
  as the warning you deferred, drop it.
- On the laptop, layers not yet started fold to their header line, matching the mockups' rows.
- The exit row sits under the pipeline and keeps the header's red blocked reason; it is where `diagnosis` is
  finally shown.
- A killed process draws as stopped, using `running.py`'s one-hour rule.
- The event additions and the one corrected field above, rather than the screen inferring from order.
- The skill pick before the meeting is a line in the amigos card, not a box.
- One row per try cycle in a layer lane; earlier cycles stay visible.
- Layers dropped from the state by hand are not drawn.
- The done-check row appears only when the run logged it.

## Left to the builder

- How `flow.py` is split (it is 731 lines and will grow; a `story_flow` module per lane is one option).
- Component split in `web/src/screens/workflow/` (a `CodeBox`, a `HoverCard`, a `PhoneList`, a `NowBar`
  are the obvious ones).
- The hover card's exact placement and delay.
- Test fixtures: AIR-47's and AIR-48's event logs are the real cases; trimmed copies under `tests/` are fine.
  AIR-48 L02's builds need `state.pre-drop-layer02-2026-11-01.json`, the state from before layer 02 was dropped.
  The mockups' timings for AIR-48 are illustrative (run `8efc` logs no `done_check`).
