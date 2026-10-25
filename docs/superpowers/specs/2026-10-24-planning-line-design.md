# The planning line: initiative → epic → stories → layers → PRs, with the human at the two ends

Status: Noah's decisions recorded 2026-10-24 (N1–N5 below). The high-level design; every sub-plan below now
has its design and plan, written 2026-10-24 and 25 and agreed with Noah: P1 `plans/2026-10-24-layer-package.md`;
P2 `specs/2026-10-24-three-amigos-design.md` + `plans/2026-10-24-three-amigos.md`; P3
`specs/2026-10-24-plans-phase-design.md` + `plans/2026-10-24-plans-phase.md`; P4
`specs/2026-10-24-specs-phase-design.md` + `plans/2026-10-25-specs-phase.md`; P5
`specs/2026-10-25-line-scheduler-design.md` + `plans/2026-10-25-line-scheduler.md`; P6
`plans/2026-10-25-pr-provenance.md`. Build order: P1, then P2 and P3, then P4; P5a after P1 and P5b after P3
and P4; P6 after P1. P1 (the layer package) is built in the branch `claude/p1-layer-package` (2026-10-25); P2 (the three amigos) in `claude/p2-three-amigos` and P3 (the plans phase) in `claude/p3-plans-phase`, each stacked on it; P4 (the specs phase) on P3, and P5a (the diagnosis and the fix cards) in `claude/p5a-diagnosis` on P1 (2026-10-25); P5b (the scheduler, the budgets, the stats, the rows) is built on the three merged together in `claude/p5b-line-scheduler` (2026-10-25); P6 is not built yet.

## Why

The superpowers flow works because each stage leaves the next one nothing to think about. A spec records
why, goals, non-goals and the decisions with their reasons. A plan turns the spec into tasks, each naming
its files, interfaces, the exact steps and the test that proves it, so the executor types and verifies rather
than designs. The human approves the spec and the plan; execution runs unattended.

airlie's story pipeline has the stages (refine, layers, build, publish) but not the property. Measured from
the traces on 2026-10-23 and 2026-10-24 (`docs/bench/2026-10-23-performance-review.md`, and AIR-22):

- The tester receives two acceptance criteria as one-line Given/When/Then, the coder's implementation brief
  and two file outlines, and designs the tests itself inside a 20-step loop with a 4,096-token thinking cap.
  On AIR-22 layer 03 that was 62 minutes, twice: 10 of 20 steps hit the cap at 200–300 s each, the first
  write came at step 15, and the reasoning restarts ("Let me organize the situation" opens three consecutive
  steps). The design happens at the slowest point, by the slowest role, cut into pieces.
- Of 57 story blocks, 27 were the final gate, 14 the pre-code check, 9 a layer that changed nothing, 5 a
  plan or story that was never usable. AIR-22 blocked after 4.8 hours on a loop variable named `l`. AIR-22
  layer 02's tester spent 40 minutes on a contradiction between two criteria that the planning stage should
  have resolved.
- Every block waits for a person. One story in fifteen has gone through unattended.

Noah's direction (2026-10-24): the superpowers flow laid over airlie's specialist roles and models; test
creation moves to the architect; the human gives input at the initiative, approves the mockups for web work,
and reviews the PRs, and everything else is automated. Build it in layers: this document, then sub-plans,
broken down until each is actionable.

The 2026-10-16 design `2026-10-16-architect-test-specs-design.md` had the architect write per-chunk test specs
for a small agent to transcribe, with mechanical red/green checks. It was approved and superseded the same day
as more trouble than it was worth, when the alternative was one coder writing tests and code. The tester seat
came after (2026-10-19), and the trace data above came after that. This design takes up that thread with what
is now known.

## Goals

- A person says what to build, once, at the initiative, picks a mockup when the work has a screen, and
  reviews pull requests. Nothing else waits on a person.
- Each stage hands the next a document that needs no interpretation: an **epic** that reads as a spec, a
  **story** with testable criteria and a named repository, a **layer** that reads as a plan task, with its
  tests written out and its implementation steps exact.
- Test design belongs to the architect, inside planning, where the code has already been read. Test
  *writing* is transcription, and transcription needs no model.
- Every hand-over has a readiness check, mechanical wherever a check can be mechanical, and a document that
  fails goes back to the stage that wrote it with the finding, not forward to a stage that will fail on it.
- Wall-clock per built layer falls: the tester's hour goes; the coder gets a package it can act on from step
  one; blocks at the pre-code check and the final gate fall because the contract was checked before anyone
  coded against it.
- The board is the control surface: every artefact is a card at its level, every stage is a status
  transition, a person can see, pause or veto any card without the line waiting on them.

## Non-goals

- A new orchestrator. The studio stays the engine; the line is the story pipeline grown upward (epics,
  stories) and sharpened at the layer. The teams and fork topologies are untouched.
- A merge path. Only a human merges; the line ends at open pull requests.
- Changing the models by design. Every seat stays a configurable role over the catalog; this document names
  which seats are cheap and which are not, and leaves the weights to `airlie.yaml`.
- The parallelism work (stories at once, redundancy in a layer). It is a separate project
  (`2026-10-23-performance-review.md`, finding 2) and the line must not assume it.
- Replacing Jira or the local board. The line uses the board's four levels and six statuses as they are.

## Anti-goals

- A line that waits on a person in the middle, with one pause by design: the mockup approval for web work
  (N4). Any other stage that cannot proceed records why on its card and the line goes on to other work; a
  person's reply, when it comes, is input to a retry, never a prerequisite for the next tick.
- An executor that can bend a test. The lock on the layer's tests stays; the executor never sees a tool that
  can change them.
- An architect that writes the implementation. It writes the tests and the steps; the coder writes the code.
  (Noah: say so if you want the architect to write code where a step is trivial.)
- A readiness check that asks a model what a program could check. Parsing, lint, red-on-base, coverage of
  criteria by tests, named files existing: programs. Whether a brief is contradictory: a model, as a last
  resort, after the mechanical checks.

## The shape

```
  initiative (a person: a paragraph in Chat, or a card)
      │  specs phase: product asks the person the brainstorming questions, then leads; the architect
      │  surveys the code; for web work ux and design draw three mockup options side by side and the
      │  person picks one (the line's one pause)
      ▼
  EPIC  = a spec: why, goals, non-goals, decisions with reasons, constraints, acceptance criteria,
          the stories it needs (titles and one-line scope), the chosen mockup attached for web work
      │  plans phase: the architect
      ▼
  STORIES = today's Story (title, summary, Given/When/Then criteria, out of scope, assumptions)
            + repository + dependencies on sibling stories
      │  three amigos, per story: architect proposes, product and tester-critic challenge, bounded rounds
      ▼
  LAYERS  = plan tasks: files to create or change; interfaces (signatures, names, messages, values);
            the layer's tests as code, in named files; implementation steps in prose, exact;
            the verification command; done-when; the criteria it makes true
      │  execution, per layer: the pipeline writes the tests (no model) → red check → coder builds
      │  (scoped tests, lock, non-thinking) → Muse review of the code → full gate → one commit
      ▼
  PULL REQUESTS, stacked, drafts, each body carrying its epic, story and layer (a person reviews and merges)
```

Mapping to superpowers: the specs phase is brainstorming plus the spec; the plans phase and the three amigos
are writing-plans at two grains (stories, then tasks); execution is executing-plans with a fresh context per
task and a reviewer after each; the human gates on the spec and the plan become readiness checks, and the
human's review moves to the pull request.

## Decisions

Noah's (2026-10-24):

- **N1.** The human gives input at the initiative and reviews the pull requests. Everything else is
  automated, with one pause: N4.
- **N2.** Test creation moves to the architect, and the architect writes the tests as code, not as specs.
- **N3.** The superpowers flow is laid over airlie's specialist roles and models, built in layers: this
  design, then sub-plans, broken down until actionable; P1 first.
- **N4.** Mockups are approved by a person before web work is built, as since 2026-10-17, and the way
  superpowers does it: ux and design draw three options side by side and the person picks one.
- **N5.** AIR-22 was killed on 2026-10-24; P1 absorbs what is useful from its branches (the lesson store,
  the sizing rule) and does not wait for it.

Mine (say so if any should change):

- **D1. Artefacts are cards with a document.** An epic, a story and a layer each live as a board item at
  their level (initiative → epic → story → subtask, the board's own levels; a layer is a subtask), with the
  document in the card's description: Markdown for people, closed by one JSON block for the pipeline, as the
  story and layer contracts already are. The state directory keeps the pipeline's working copy as today.
  The local board is the line's board in the first cut: the Jira adapter reads, moves and comments today
  and has no create, and creating epics and stories in Jira (issue types, parent links, a description
  limit against a package that holds test code) is its own later project. With `story.board: jira` a story
  still enters at the story level as it does today.
- **D2. The architect writes the layer's tests as code**, not as specs. A test is the layer's design and
  its contract, the architect has read the code and the nearest test module during planning, and the
  2026-10-16 objection (one long final turn) does not apply when each layer is planned in its own call. The
  pipeline writes the test files from the package and checks them; no tester agent runs, and the tester
  seat as an author is retired (`story.tester_role`, `_amend_tests` and the tester's mend go with it; the
  lock by definition stays, now locking the architect's tests to the coder). The tester role survives as
  the critic seat in the three amigos (D5), a role prompt, not an executor.
- **D2a. Who repairs what, after a package is accepted.** A package whose tests fail the readiness check
  (parse, lint, imports, red on the base) is a defective package: it goes back to the architect's layer call
  with the finding, up to a budget, never to another agent. A final gate that fails after the coder built is
  first the coder's: one follow-up with the gate's output, as today. If it still fails, the package goes
  back to the architect with the coder's account and the gate's tail, and the architect decides: a test
  that was wrong (fix it; the lock is the architect's own), or a layer that was too big or mis-split
  (re-plan it, and the lesson is recorded, D7). A blocked card that the tick resumes is re-planned from the
  architect's layer call with the block's lesson, not rebuilt from the same package.
- **D3. The coder writes the implementation from steps, never from the tests alone.** A layer package gives
  it the files, each new or changed function with its signature, the behaviour per step with the exact
  names, messages and values the tests assert, and the order. The coder runs non-thinking on the executor
  seat. The architect's brief for a layer is the package; there is no second prose brief.
- **D4. Readiness checks gate every hand-over, mechanical first, at two moments for a layer.** At the
  story's planning, the layer *split* is checked: each criterion the story owns is assigned to one layer,
  each layer names its files and the interfaces at its boundary (the names layer 02 will import from layer
  01), and each fits the size rule (one concept, about three source files, one test file, inside the coder's
  step and context budget, the rule AIR-22 adds). Just before a layer is built, on the branch the layers
  below it actually produced, its *package* is checked: every file it names exists or is marked new; every
  test file parses; `ruff check` passes on the test files after the fixer; every import in the tests
  resolves in the worktree or names something the package's steps define; each criterion the layer owns has
  at least one test; every test fails on the layer's base (the existing pre-code check) and no existing test
  breaks. The second check cannot run at planning time: a layer-02 test that imports a name layer 01 adds
  has nothing to import until layer 01 is built. A story is ready when every criterion is a testable
  Given/When/Then, the repository is named, the
  scope names paths that exist, and no two criteria contradict each other (a model check, after the
  mechanical ones). An epic is ready when it has goals, non-goals, criteria, constraints and the stories it
  needs, and product has no open question for the person. A document that fails goes back to its author
  with the finding, up to a budget, then its card goes to `blocked` with the finding and the line continues
  with other cards.
- **D5. The three amigos meet once per story, and a critic reads each layer's package.** The meeting is one
  AutoGen group chat (Noah, 2026-10-24): product, the architect and the tester in one conversation, any
  agent addressing any other, with airlie's mechanical checker as a fourth participant, until the three agree
  on the requirements, the changes (the layer split with its boundary interfaces) and the proof (a test plan
  per criterion at the right level of the repository's test pyramid), or until a turn cap, when each owner's
  version of its own part stands. Just before each layer is built, the architect writes that layer's package
  on the real base (D4's second moment) and the tester critic reviews the tests as code, in one turn, before
  the mechanical checks run. Planning is therefore interleaved with building, and D9's budget is attributed
  per layer. The tester runs on the cross-family reviewer model (Muse), as the reviewer does today, since a
  critic of the same family agrees too readily; product stays on the generalist. Design:
  `2026-10-24-three-amigos-design.md`.
- **D6. The specs phase begins with product asking the person the brainstorming questions.** Noah's input
  at the initiative is a dialogue, not a form: product asks what the moment is, what success looks like,
  what would make it a failure, one question at a time in Chat or as card comments, then writes the epic.
  The architect's survey (today's `_survey`) runs beside it. For web work, ux and design draw three mockup
  options that differ from each other, shown side by side on the epic's card and in Chat (the visual
  companion of superpowers' brainstorming), and the epic waits in `review` until the person picks one or
  comments; a comment is input to another round of three. An epic with no screen does not wait (N4). The
  chosen mockup is attached to the epic and travels into its stories' briefs, as the design role's output
  does today.
- **D7. Lessons feed planning.** The per-repository lesson store and the sizing rule that AIR-22 built on
  its branches (`AIR-22-record-size-blocked-layer-less-01-lessons-store` and `-02-brief-sizing-lessons`:
  `story/lessons.py`, the hub rules `brief-layers-sizing` and `brief-layers-lessons`, their tests) are
  absorbed into P1 (N5); AIR-22's unbuilt layer 03, the pipeline wiring, is replaced by D2a's repair policy,
  which records a lesson whenever a package comes back or a layer is re-planned. The amigos read the recent
  lessons for the repository.
- **D8. The executor seat is a config choice judged by wall-clock to a built layer.** `line.executor` names
  the coder's role; its model is the generalist without thinking today and the fast MoE (`coder8`) as the
  experiment, since a package that leaves nothing to design is where a fast model should win.
- **D9. Each stage has a token and wall-clock budget**, recorded per card: planning time moves upward on
  purpose, and the line must show that the whole got cheaper, not that the cost moved.
- **D10. One line, one scheduler.** `task story:tick` grows into `task line:tick`: it takes the top ready
  card at each level, in order (an initiative to spec, an epic to plan, a story to the amigos, a layer to
  build), one piece of work at a time until the parallelism project lands. Ready keeps today's meaning: a
  card a person made or imported is ready when a person sets it `approved` (giving the initiative is that
  act; an imported Jira backlog sitting in `todo` is never built unasked); a card the line itself made from
  a parent is ready at birth. A person moves a card to `blocked` to pause it and back to `approved` to
  resume it; the tick resumes a card the line blocked up to a budget before it leaves it for a person.

## Constraints

- Local models: the generalist decodes at 15–17 tok/s at 26k context with a 4,096–8,192 thinking budget;
  the MoE at 60–110. Planning calls are long thinking turns; executor calls must be short.
- The board's levels and statuses (`initiative/epic/story/subtask`; `todo/approved/doing/review/blocked/done`)
  are the vocabulary; `approved` is a person's go on a card they made (D10), and the only one the line
  needs is on the initiative (N1).
- The repository's `AGENTS.md` test gate decides a layer, as today; every repository the line builds in has
  one.
- Everything stays a Taskfile task under uv; every seam is a key in `airlie.yaml`; model-facing prose lives
  in the prompt hub; unknown keys are errors.
- The one-story lock, the worktree per layer, the lock on tests, the stack of draft PRs and "only a human
  merges" stay as they are.

## The sub-plans

In the order to build them, each its own design and plan. Each names what it produces, what "done" is, and
what it depends on.

**P1. The layer package and the executors.** Replaces the tester subtask. Produces: the layer contract
(D1, D3: files, interfaces, tests as code, steps, verification, done-when, criteria); the architect's
per-layer package call, made in `_build` just before the layer runs, on the branch the layers below it
produced (D4's second moment; the split stays today's `_layers` for now, and the three amigos of P2 wrap
both calls later); the pipeline step that writes the tests into the worktree, locks them and runs the
mechanical readiness checks, returning findings to the architect's call up to a budget; the coder as
executor on the package (D3, D8); the repair policy (D2a), which retires `story.tester_role`, the tester
subtask and `_amend_tests`. Done when: a story runs end to end with no tester agent; a layer's wall-clock
to built is measured against the 2026-10-23 numbers; the pre-code findings move from "the tester's retry"
to "the package came back" events; the lesson store from AIR-22's branches is in and records those
returns (D7). Depends on nothing; builds on `story/briefs.py`, `story/layer_checks.py`,
`story/tester_lint.py`, the lock (`studio.lock`) and `story/model.py`, and takes `story/lessons.py` and the
two hub rules from AIR-22's branches (N5).

**P2. The three amigos.** Produces: the critic seats and the bounded conversation (D5); the readiness
verdict as a JSON contract; the story-level checks (criteria testable, no contradictions, scope paths exist);
lessons read into the conversation (D7). Done when: a story whose criteria contradict each other (AIR-22
layer 02's AC3 against AC4) comes back at planning, not at the tester; every objection and whether it was
later borne out is in the story's events (the stats over them are P5's). Depends on P1. Design:
`2026-10-24-three-amigos-design.md`.

**P3. The plans phase: epic to stories.** Produces: the epic contract read by the architect; stories as
cards under the epic with dependencies; the story readiness check; the tick building stories in dependency
order (D10). Done when: an epic with three stories becomes three story cards whose criteria the amigos
accept without a person touching them. Depends on P1 (the story contract is today's, so P3 can be designed
beside P2).

**P4. The specs phase: initiative to epic.** Produces: product's dialogue with the person at the initiative
(D6), in Chat and as card comments; the epic contract (a spec: why, goals, non-goals, decisions, constraints,
criteria, stories); the architect's survey; for web work, ux and design's three mockup options side by side
and the pause until the person picks one (N4, D6); the epic readiness check. Done when: a paragraph from
Noah becomes an epic card he recognises as what he meant, with the questions product asked recorded on it,
and a web epic shows him three mockups and carries the one he picked. Depends on P3's epic contract.

**P5. The line's scheduler and control surface.** Produces: `task line:tick` over all levels (D10), the
pause/veto/resume semantics on the board, the resume budget for blocked cards, per-stage budgets and
their events (D9), the web app's board and workflow screens showing the line. Done when: the launchd agent
runs the whole line and a person's only actions in a week are initiatives and PR reviews. Depends on P3 and
P4; its scheduler part can start after P1.

**P6. Provenance in the pull request.** Produces: PR bodies that carry the epic, the story, the layer
package and the checks that passed, so the human review has the why beside the diff; the layer's tests
named as the contract. Done when: a reviewer can judge a PR without opening the board. Depends on P1; small.

Later, outside this design: parallel stories and redundancy within a layer (the performance review's
finding 2); review comments on a PR feeding a fix layer back into the line.

## What "actionable" means for the sub-plans

A sub-plan is actionable when it reads as a superpowers plan: tasks in order, each with its files, the
interfaces it adds or changes, the failing test to write first, the steps, and the gate to run; nothing in a
task needs a decision the plan does not record. A sub-plan whose task cannot be written that way is not
ready and gets its own design first. P1 and P6 are expected to go straight to plans; P2, P3 and P4 each
want a short design of their own (contracts and prompts) before their plan; P5 wants a design once P3 and P4
exist.

## Left to the builders

- The JSON field names of the epic, story and layer contracts, and how each renders into a card and a brief.
- How the architect's layer call is bounded (steps, thinking budget, max_tokens for a package with tests).
- The exact mechanical checks and their order, and which findings return to the author versus block the card.
- The critics' prompts and the shape of their verdicts.
- The product dialogue's question set and how a reply reaches a retry.
- How a layer package's tests are written into the worktree (new file, or appended to an existing module
  the architect names) and how that interacts with the lock by definition.

## Decided with Noah, 2026-10-24

1. AIR-22 was killed; P1 absorbs its lesson store and sizing rule from its branches and does not wait (N5).
2. The architect writes the tests as code, not as specs (N2, D2).
3. Mockups for web work are approved by a person, three options side by side, the line's one pause (N4, D6).
4. P1 first (N3).

Settled by me after the builder check (say so if any should change): the package is written just before
its layer is built, on the real base, and checked twice (D4, D5); the tester seat as an author goes in P1
and the repair policy is D2a; the local board is the line's board first and Jira's create side is a later
project (D1); ready keeps its meaning and an imported backlog is never built unasked (D10); the executor
seat defaults to the generalist without thinking with the MoE as the experiment (D8).
