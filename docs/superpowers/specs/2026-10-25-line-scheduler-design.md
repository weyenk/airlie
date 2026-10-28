# The line's scheduler and control surface (P5 of the planning line)

Status: agreed with Noah on 2026-10-25 (its decisions N1 to N5 are his). Built: P5a (D4 to D6, D12) in
`claude/p5a-diagnosis`, P5b (D1 to D3, D7 to D11) in `claude/p5b-line-scheduler` (2026-10-25); the unattended story
run on the real engine (plan Task 13, step 4) is the person's. Plan:
`docs/superpowers/plans/2026-10-25-line-scheduler.md`. Parent: `2026-10-24-planning-line-design.md`
(D9, D10, and its sub-plan P5). Builds on the supervisor triage design (`2026-10-22-supervisor-triage-design.md`,
whose layer 3 this absorbs) and on the tick steps P3 and P4 added. Its plan follows once this is agreed.

## Why

Four phases have been designed as separate pipelines over one 15-minute tick that was written for stories
alone. P3 and P4 each bolted steps onto it. What is missing is the thing that makes them one line that runs on
its own: a scheduler that knows every level, uses the engine's headroom where it exists, and tells a person
what happened when it stops.

Noah's account of the last month decides the shape. He steps in when he sees a story in `blocked`. He asks a
Claude session why, and the session reads the traces. Nearly every time the cause is a bug in airlie's own
mechanics, not in the model's work. So the first thing this design delivers is that answer, written by the
line at the moment it blocks, with the bug filed as a card on airlie's own board. Self-healing in full, the
line fixing and shipping itself, waits for a discipline that is not there yet; the line files its bugs and
never merges its fixes.

The engine decides the second thing. The October bench shows that short requests batch almost for free, four
streams at 4k context giving 3.6 times the throughput of one, while long requests gain nothing, two streams at
32k giving less than one. So the line runs two lanes, one building and one planning, and fans out only the
short asks inside each. Several stories at once wait until one story goes through unattended as a matter of
course, which is this design's done-when.

## Goals

- One scheduler, `task line:tick`, over initiatives, epics, stories and layers, with two lanes: a build lane
  that runs one story's build at a time, and a planning lane that runs one planning job beside it, each
  fanning out its short asks.
- A block writes its diagnosis: a mechanical evidence pack, a cause class from deterministic rules, Muse's
  reading of the pack, and the paths a person or a Claude session needs next; shown on the card, printed by
  `task line:why`, answered by `/why` in Chat.
- A block whose cause is airlie's own code or configuration becomes a fix card on airlie's board, in `todo`,
  deduplicated by signature, written so that either a Claude session or the line can take it; when the
  fix merges, the stories it blocked resume on their own.
- A transient failure retries within a small budget; the model's own failures follow P3's resume-once-then-
  re-plan; an environment failure waits for a person with a note.
- The board's statuses are the controls: a person pauses and resumes any card and everything under it, parks
  with `todo`, and holds the whole line with one switch while running work finishes.
- Every stage's tokens, wall-clock and steps are recorded per card against a budget, with warnings and
  overruns as events, so the line can show that the whole got cheaper when planning time moved upward.
- The numbers that judge the done-when: the unattended rate (stories that went from approved to review with
  no person's action in between), blocks by class and category, hours per story against estimate, the amigos'
  objections and how many were borne out.
- The flow API rows and routes the Vue app renders: the line's state, the two lanes, the diagnosis, the
  switch.

## Non-goals

- Several stories building at once, or redundancy within a layer (Noah: after unattended single stories).
- The line merging its own fixes, restarting the engine on its own word, or approving its own cards.
- The needs-you list as a screen of its own (Noah: set aside; the board's columns and the delivery notes
  carry it for now).
- Card actions beyond the statuses (retry from a stage, switch a profile): later, once the diagnosis shows
  which ones are wanted.
- A weekly report document: the stats command is enough until there are weeks to report.
- The Vue screens themselves: P5 provides rows and routes; the Vue work renders them.
- Enforcing budgets by stopping a stage: P5 records and warns; the stage caps that exist (steps, turns,
  attempts) remain the only stops.

## Anti-goals

- A diagnosis that blames the model for airlie's bug: the mechanical class is read first, and Muse's reading
  never erases it.
- A fix card the person cannot act on: one without the paths, the run, the step and the evidence excerpt.
- Duplicate fix cards for one bug, or a story resumed into the same bug twice without a person seeing it.
- A planning job that makes the build lane crawl: the planning lane yields when the build's decode falls
  under the floor.
- Two schedulers racing: the serve ticker and the launchd agent take the same lane locks.
- A paused line that keeps picking work, or one that kills work mid-layer when paused.

## The shape

```
  every beat (serve: every line.tick_s; launchd: every 15 minutes, as the belt)
      │
      ├─ gates: engine up and running the story profile; GitHub answers; the line is not paused
      ├─ bookkeeping, no model: promote dependents, replay moved bases, move epic and initiative statuses,
      │     close estimates, resume stories whose fix card merged, retry transient blocks within budget
      │
      ├─ PLANNING LANE (one job, lock plan.lock): the oldest ready job in this order:
      │     an initiative's turn or an epic's revision (P4) · an epic to plan or re-plan (P3) ·
      │     a story to plan: refine, the three amigos, the layer split (P2); its short asks fan out,
      │     the width governed by the build lane's decode
      │
      └─ BUILD LANE (one job, lock build.lock = today's story_lock): the top planned story: its layers,
            each with its just-in-time package (P1), the gate, the review, the publish; a restack or replay

  a block, in either lane
      │
      ├─ the evidence pack (code): reason, stage, layer, attempt, run, stop reason, refused calls, holds,
      │     repeats, continuations, tokens, minutes, the gate's tail, tracebacks, engine and GitHub events,
      │     the diff stat, the paths (events, trace, the last steps)
      ├─ the class (rules): transient · environment · content · process · config, and a signature
      ├─ Muse's reading of the pack (the supervisor's block trigger, with the pack), kept beside the class
      ├─ written: stories/<key>/diagnosis/<n>.{md,json}; the card's block note; `task line:why`; /why
      │
      ├─ transient → retry within budget        environment → a note, waits for a person
      ├─ content   → P3's resume once, then re-plan
      └─ process, config → a fix card on airlie's board (todo), or a link to the open one;
                           the story is labelled blocked_by:<fix>; it resumes when the fix merges
```

## Decisions

Noah's (2026-10-25):

- **N1.** Two lanes. The build lane runs one story at a time; the planning lane runs one planning job beside
  it: an initiative's turn, an epic's planning, a story's three amigos. Inside either lane the short asks fan
  out. Several stories at once wait until single stories complete unattended as a matter of course.
- **N2.** Blocks are nearly always airlie's own bugs. The first deliverable is the diagnosis: what Noah asks a
  Claude session for today, written by the line when it blocks.
- **N3.** The needs-you list is set aside for now.
- **N4.** Self-healing in full waits for more discipline than the line has; the line files its own bugs and
  never merges its own fixes (only a human merges, standing since October).
- **N5.** Gaps only: the planning lane runs its long turns only in the build lane's gaps and idle time; only
  the short asks ride beside the build's decode.

Mine (say so if any should change):

- **D1. One scheduler, two lanes, two locks.** `task line:tick` replaces `story:tick` (kept as an alias for a
  release). A beat runs the gates, the bookkeeping, then one job per lane. The lanes are two `fcntl` locks in
  the stories directory: `build.lock`, which is today's `story_lock` under its new name, and `plan.lock`. A
  job takes its lane's lock for its whole run, whether the tick, the serve routes (`story_job`,
  `initiative_job`, P4's turn route) or `task story -- KEY` started it, so two schedulers never run the same
  lane. The planning lane's order is fixed: a person is waiting on the first two (an initiative's turn, an
  epic's revision), the epic's planning feeds the board, and a story's planning feeds the build lane. The
  build lane takes the top `approved` story whose planning is done (its state's stage is `layers`), as today's
  tick takes the top approved story. `StoryPipeline.run` splits at that boundary into `plan(card)` (refine or
  the story card, the amigos, the layer split; P2's stage name `layers` stays the boundary) and `build(card)`
  (the layers with their packages, the gate, the review, the publish); `run` is the two in sequence, for `task
  story -- KEY` and the tests.
- **D2. The planning lane's long turns run in the build lane's gaps (N5).** The build lane writes its phase
  to `stories/build.phase` (`decoding` while a role's turn is in flight, `gap` during the gate, the review,
  the publish and between layers, `idle` with no story building; with a timestamp), since the two lanes may
  be different processes. A planning job is a sequence of long turns (product's and the architect's turns,
  the amigos' turns) and short asks (consultations, surveys, reads, mockup renders, the checker). A long turn
  starts only while the phase is `gap` or `idle`; the job polls the file every two seconds until it is. A turn
  that has started runs to its end even when the build leaves the gap, so the overlap per layer is at most one
  turn's tail; the job then waits again before its next long turn. A planning job itself starts only when the
  phase is `gap` or `idle` at the beat. The short asks ride beside the build's decode, governed by the floor:
  the build lane's last five steps carry `gen_ms` and tokens, and under `line.decode_floor_tps` (12) the fan-
  out width is 1, else `line.fanout` (4); the `Asker`'s gather takes a `fanout: Callable[[], int]`, which
  P4's rounds and P2's checker use. Events `lane_wait` (a long turn waiting for a gap, with the seconds
  waited when it starts), `lane_yield` and `lane_resume` on the line's log. The engine flags the performance
  review named (chunked prefill, explicit prefix-cache memory, stored-KV quantization) are prerequisites for
  the riders not to stall the build on their prefill; the plan turns them on in the story profile first and
  the doctor's gate (D10) checks them.
- **D3. The serve process ticks, the launchd agent is the belt.** `task serve` runs the same `tick()` every
  `line.tick_s` (60) seconds in a background task when `line.serve_ticks` (true), so a person's move or answer
  is acted on within a minute while serve is up; the launchd agent keeps its 15 minutes and runs `airlie line
  tick`, the same function in another process. The lane locks make the two safe together. The beat runs
  on the serve loop, with its blocking helpers (git, gh, SQLite) in worker threads as the serve offloading
  rule says (performance review 9), and every job a tick starts is registered in the serve process's `Jobs`
  (kind `line`), so the Workflows screen follows a tick-started build as it follows one a route started.
- **D4. The diagnosis: an evidence pack, a class, Muse's reading, four doors.** When a story blocks in either
  lane, code writes `stories/<key>/diagnosis/<n>.md` and `.json` (`n` counts blocks of that story). The pack
  and the class are computed from disk alone, by a `Diagnoser(state_dir, key)` over `state.json`,
  `events.jsonl` and the run directory, so a past block can be diagnosed too: `task line:why` on a story with
  no `diagnosis/` directory makes one, the rules are calibrated on the 57 recorded blocks before they go live
  (the performance review's table is the expected distribution), and those blocks are the test fixtures. The
  pack:
  the block reason, stage, layer and attempt; the studio run id and subtask; the stop reason (`cut_off`,
  `repeated_action`, `max_steps`, `ok`); counts from the run's events of refused tool calls, holds that fired
  (`write_deadline`), repeats, fresh continuations, steps, tokens and minutes; the gate's last 60 lines; any
  `error` or `*_failed` event and any traceback; engine and GitHub events (today's `engine_error`,
  `fetch_failed`, `read_refused`, `write_deadline`; new kinds only where no event records the fact today); the
  diff stat of the layer's worktree against its base; and the paths: the events log, the run directory, the
  subtask's trace, the last five step files. The class, by rules in this order, each naming the evidence it
  matched: `transient` (a fetch, GitHub or engine failure, a stall); `process` (a traceback through
  `airlie.*`, or a role that ended `repeated_action` after three or more refused calls under a hold, or a
  `story_failed` event); `config` (a `cut_off` at the role's `max_tokens` or thinking budget, or a
  `max_steps` end with the plan unfinished); `environment` (the failing gate commands also fail on the base
  tree, `_failing_at`); else `content`. A `signature` for dedup: `<class>:<kind>:<where>`, where `kind` is the
  rule's name or Muse's category and `where` is stable across edits: the exception type and the airlie
  `module:function` from the traceback's innermost airlie frame, the hold's name, or the failing test id;
  never a line number, since a fix moves lines and the same bug must keep its signature. Then Muse: the supervisor's `block` trigger moves from inside `_build` into the block
  path, after the pack exists, gets the pack as evidence and answers as today, plus two fields, `class_agrees: true | false` and `class_reason`; the ledger's `interventions` table
  gains `class`, `signature` and `class_agrees` columns (a migration on open); both readings are kept and
  shown, the mechanical class first. The card's block note is the diagnosis's first section: the class in
  one line with its evidence, Muse's sentence, and what happens next (a retry, a fix card's key, a wait).
  `task line:why -- KEY [--n N] [--paths]` prints the latest diagnosis (or the nth) and, with `--paths`, only
  the paths, for a Claude session to read. `GET /v1/airlie/stories/{key}/diagnosis` returns the JSON and the
  Markdown. The Chat skill `/why KEY` hands the chat role the Markdown as context (`action: why`; the app
  fetches the route and prepends it as data), so the first answer comes from the line. The supervisor stays
  advisory inside the run as its design says; the diagnosis is a step the pipeline's block path runs after
  it, never a stop of its own: a pack that cannot be gathered is a `diagnosis_failed` event and the plain
  block note of today.
- **D5. A fix card for airlie's own failures.** For class `process` or `config`, the block path looks for an
  open card (not `done`) on the local board with the label `signature:<signature>`; finding none, it creates
  one: level `story`, repository airlie's own (`line.home_repo`, default the repository the line runs from,
  as its remote names it), labels `fix`, `repo:<home>`, `signature:<sig>`, `blocks:<KEY>`, status `todo`,
  title `Line: <KEY> blocked at <stage or layer NN>: <kind>`, description in sections: Why (what blocked and
  where, in two sentences); Evidence (the pack's excerpts: the traceback or the refused calls, the gate's
  tail, the counts); Hypothesis (the rule that matched, Muse's diagnosis and its `fix_if_attempt`); Reproduce
  (`task line:why -- KEY --paths`, the run id, the step files, the command that failed); Acceptance criteria,
  Given/When/Then as far as the evidence allows (for a traceback: "Given the inputs of run R at step S, when
  the pipeline runs that step, then it does not raise and the layer continues"; for a hold: "Given a layer
  where the coder must not write tests, when its write deadline passes, then no hold fires"); Out of scope
  (the story that blocked). The blocked story gets the label `blocked_by:<fix key>` and its note names the
  card; the fix card gets a comment naming each story it blocks as they arrive. Both doors are open: a person
  approves the card and the line plans and builds it (P2's amigos refine the criteria from the evidence), or
  a person opens a Claude session on it with `task line:why --paths`; the card is the same. A fix card is
  never created for class `content`, `transient` or `environment`.
- **D6. After the fix, the line resumes what it blocked.** A merge on GitHub changes nothing the line runs:
  the tick runs from the home checkout and `task serve` is a long-lived process. So "the fix merged" means
  the fix is in the code the line runs: the merge commit is an ancestor of the home checkout's trunk and the
  serve process's own commit (`build_commit()`) is at or past it. Bookkeeping on every beat: a fix card whose
  PRs are merged on GitHub (read as P3's promotion reads them) but not yet in the checkout gets one comment,
  "merged; run `task line:deploy` to pull and restart serve, then AIR-123 resumes", and a `fix_awaiting_deploy`
  event; `task line:deploy` pulls the home checkout's trunk and restarts serve (the launchd tick needs nothing:
  a new process from the checkout each beat), and the line never pulls its own checkout (N4). Once the fix is
  in the running code, the fix card is `done` and each story labelled `blocked_by:<its key>` is released: the story moves to `approved` with a note and a `resumed_after:<fix key>` label, and
  its next build rebuilds the stopped attempt as a resume does today. A story that blocks again with the
  same signature after such a resume is left for a person (its note says so) and the fix card gets a comment:
  the loop stops at one. Transient blocks retry up to `line.transient_retries` (2) per signature, counted by
  `retried:<sig>:<n>` labels, then wait for a person. Content blocks follow P3 D8 (resume once, then the
  epic re-plans; a story with no epic is resumed once then waits). Environment blocks wait for a person.
- **D7. The board's statuses are the controls, plus one switch.** As planning-line D10: a person moving any
  card to `blocked` holds it and everything under it (`LocalBoard.next_*` read every ancestor's status);
  `approved` or `doing` releases; `todo` parks; `done` closes. The one switch is `line.paused`: a file
  `<state_dir>/line.paused` written by `task line:pause` and removed by `task line:resume` (and by `POST
  /v1/airlie/line {"paused": true|false}`), read at the top of every beat: paused, the tick does the gates and
  the bookkeeping and starts no job; a running job finishes. The tick's log line says `paused`.
- **D8. Budgets are recorded and warned, not enforced.** `line.budgets.<stage>` carries `wall_s` and `tokens`
  per stage (`survey`, `refine`, `questions`, `imagine`, `amigos`, `plan`, `package`, `layer`, `review`,
  `publish`), defaults from the October ledger's medians doubled. A `StageMeter` folds the run's step events
  into `stages/<stage>.json` in the card's state directory (tokens in and out, wall-clock, steps, attempts),
  emits `budget_warning` at 80 percent and `budget_exceeded` at 100 percent on the card's events with the
  figures, and posts one note at the overrun. Nothing stops: the stage's own caps stop it. The per-card
  figures are what `line:stats` and the flow rows read; planning-line D9's test, that the whole got cheaper,
  is `line:stats` over two weeks.
- **D9. The stats that judge the done-when.** The board records who moved what: `BoardStore.update` and
  `move` append to a `moves` table (key, from, to, by, at), `by` the API's principal for a person and `line`
  for the tick and the pipeline (`LocalBoard.move` passes it), so a person's move is never inferred from
  labels. `task line:stats [-- --since DATE]` prints: the unattended rate (stories that went `approved` to
  `review` with no person's comment or move between, from `moves` and the comments); blocks by class and by category, with how many filed a fix card and how many
  resumed after a fix; hours per story against its estimate (P4's `StoryCosts`); the amigos' objections by
  role and part and the share borne out (`amigos_borne_out` events; this is the `amigos:stats` P2 left to
  P5); budget overruns by stage. The same figures at `GET /v1/airlie/line/stats`.
- **D10. The engine must be running the story profile.** The doctor compares the running engine's flags
  (`/v1/models` and the process's arguments) with the story profile's engine spec (performance review 7) and
  reports a mismatch; the tick treats a mismatch as a gate (`engine_mismatch` event and a log line naming
  `task restart -- --topology <profile>`) and starts no build, since a KV smaller than the profile's rotates
  a long prompt and wastes a layer. The doctor's memory projection counts the two lanes' sequences
  (performance review 8's leftover): the build lane's one at the coder's context plus `line.fanout` asks at
  the consult context.
- **D11. The line's record and rows.** `<state_dir>/line/events.jsonl` holds the scheduler's own events
  (`tick`, `gate_failed`, `lane_start`, `lane_done`, `lane_yield`, `lane_resume`, `paused`, `fix_filed`,
  `fix_linked`, `resumed_after_fix`, `transient_retry`, `engine_mismatch`). `GET /v1/airlie/line` returns
  `{paused, gates: {engine, profile_match, github}, lanes: {build: {card, title, stage, layer, step, tps,
  elapsed_s, estimate_h} | null, planning: {...} | null}, queue_hours, blocked: [{key, class, kind, fix}],
  last_tick}`; `GET /v1/airlie/line/events` the log's tail. The story flow row gains `diagnosis` (the latest
  class and kind) and `budgets` (the per-stage figures). The Vue work renders the line and the diagnosis.
- **D12. Two halves, buildable apart.** P5a, the diagnosis and the fix cards (D4, D5, D6's resume, `line:why`,
  `/why`), depends on the pipeline and the supervisor and is built right after P1 lands, on P1's block path
  and event names, not beside it (P1 rewrites the very cases the rules read); its rules are calibrated on
  the recorded blocks either way. It is the deliverable Noah named first. P5b, the scheduler (D1, D2, D3, D7), the budgets (D8), the stats
  (D9), the engine gate (D10) and the rows (D11), depends on P3 and P4 for the levels it schedules; its lane
  locks and the serve ticker can land after P1 with the planning lane empty.

## Constraints

- The planning-line constraints apply. A diagnosis quotes traces as data; a fix card's text never reaches a
  shell; the home repository comes from config or the checkout's remote, never from a card.
- `BoardStore` and the board API are the only writers of cards; the fix card and the labels go through them.
  With `story.board: jira`, the fix card is a note on the blocked issue and the labels are not written.
- The supervisor's ledger stays append-only; the new columns arrive by `ALTER TABLE ADD COLUMN` on open,
  with the rows before them reading as null.
- The two lane locks are the only global locks; a job holds exactly one. `task story -- KEY` takes
  `build.lock` for its build and `plan.lock` for its planning, in turn.
- Do not restart the engine from the line; the engine gate names the command for a person.
- No live engine or network in tests; the tick is tested with a fake board, fake lanes and scripted
  engines as the story factory's tests are.
- The front end is the Vue app's work: routes and rows only.

## Left to the builder

- The exact evidence-pack fields and the Markdown layout of a diagnosis; the wording of the block note.
- The rule order's edge cases (a traceback and a refused-call loop in one run: the traceback wins) and the
  `where` of a signature when no module, hold or test id is in the evidence (the stage name).
- How `plan(card)` and `build(card)` share `StoryState` and the resume semantics across the boundary.
- The step-event fields the decode floor reads, and the window when the build lane has fewer than five steps.
- The ledger migration and how `supervisor:log` shows the new columns.

## Decided with Noah, 2026-10-25

1. Two lanes, build and planning, with fan-out inside each; parallel stories after unattended single stories
   (N1).
2. The diagnosis tooling first: the line writes what Noah asks a Claude session for today (N2).
3. The needs-you list set aside (N3).
4. The line files its own bugs and never merges its fixes (N4).
5. Gaps only: planning's long turns run in the build's gaps and idle time; the short asks ride beside the
   build (N5, D2).

Settled by me (say so if any should change): the fix card is written for both doors, a Claude session and
the line, and waits in `todo` (D5); the diagnosis's mechanical class is read before Muse's and both are kept
(D4); the short asks' fan-out yields to a decode floor of 12 tokens a second and a started long turn runs to
its end (D2); serve ticks every minute and
launchd stays as the belt (D3); budgets are recorded and warned, never enforced, in P5 (D8); one switch pauses
the line, statuses do the rest (D7); a story resumed after a fix that blocks again on the same signature
waits for a person (D6); the engine's profile mismatch is a gate (D10); P5 splits into the diagnosis half,
built right after P1, and the scheduler half, after P3 and P4 (D12); the diagnosis is computed from disk and
calibrated on the recorded blocks, Muse's block triage moves into the block path, and a signature names a
module and function, never a line (D4); "merged" means deployed to the checkout and the serve process, with
`task line:deploy` as the person's one step (D6); the board records who moved a card (D9); the serve beat runs
on its loop with tick-started jobs registered (D3).
