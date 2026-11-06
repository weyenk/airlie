# Generic nodes: the story pipeline rebuilt from reusable steps

2026-11-06. Brainstormed with Noah in chat. No mockups: nothing on a screen changes.

## Why

Every workflow airlie runs today is a hand-written Python sequence. The story pipeline is one method,
`StoryPipeline._drive` (`src/airlie/story/pipeline.py:623-710`), calling about thirteen private steps in
a fixed order, with the layer build further bundled inside `StudioOrchestrator.execute_task`
(`src/airlie/topology/studio.py:851`). The initiative and epic lanes are two more hand-written sequences.
When the next kind of work arrives (a bug fix, a spike, a research task, a design-only story), the
pipeline has to be redesigned from scratch, because none of its boxes exist on their own.

Noah's direction (2026-10-31): isolate every pipeline box first, so that workflows compose from the
boxes instead of each work type getting its own custom flow. This design does that for the story
pipeline. It does not add a second workflow. It makes the second workflow cheap.

## The rule

**A node type is a protocol. A node is config. A workflow is wiring.**

- A **node type** fixes how the model or models act inside the box: how they take turns, what each one
  sees of the others, and what ends the box. Two boxes are different types only when that difference
  cannot be configured by the user. Adding a type is code.
- A **node** is one type plus the settings a user can give it: the role (or roles), the prompt, the
  inputs, the outputs and the step count. Adding a node is config.
- A **workflow** wires nodes together: sequence, parallel, loop back, branch, and for each. A
  workflow can be saved and dropped into another workflow as one box.

Noah stated the rule on 2026-11-06: "the nodes are different when the way the model(s) act inside the
node is different, and that difference can't be configured by the user."

## Goals

1. The story pipeline runs as a workflow file of nodes, and behaves the same as today: same prompts,
   same roles, same retries, same artifacts, same events.
2. Every box in the story pipeline is a node that another workflow could use unchanged.
3. The layer build is several nodes (write, done-check, review, gate, squash), not one.
4. The same events reach `events.jsonl`, so the pipeline screen, restart-from-step and the supervisor
   ledger keep working with no change.
5. Proven by clean bench runs of the roman stories on the composer, with the old driver still present
   behind a flag until the flip.

## Non-goals

- No second workflow in this round. Bug fix, spike, research and design-only stories come after.
- The initiative and epic lanes are left as they are. They are the obvious second and third
  workflows, and they get rebuilt from these nodes later.
- No change to any prompt, brief recipe, role, model or limit. If a node's behavior differs from the
  box it replaces, that is a bug in the node.
- No change to the web app. The screen draws the same boxes from the same events.
- No workflow editor. Workflow files are written by hand.
- The Team and Pair types are named so the rule is seen to hold for them, but they are not built.

## Anti-goals

- **A type per box.** A "survey node" and a "refine node" would be two types for one protocol. That
  is the failure this design exists to prevent. If a difference can be put in config, it is config.
- **Loops as nodes.** Sending the reviewer's findings back to the writer does not change how either
  model behaves, so the loop is a wire with a bound, not a node type.
- **Nodes sharing state by accident.** Today the coder, done-check, reviewer and gate share one live
  worktree inside one method. After this, every node names what it reads and writes, and the composer
  refuses to run a node whose inputs are not there.
- **A rewrite of the model-facing code.** The node runners wrap the functions that exist today
  (`Asker.usable`, the held conversation, `Meeting`, the coder's loop, `PackageChecker`). They do not
  re-implement them.

## The node contract

Every node, of every type, is a record with these fields. Noah named the first five; the rest are
my calls, marked.

| Field | Meaning |
|---|---|
| `type` | One of the node types below. |
| `role` | The seat that runs it, resolved through `config/roles.yaml`. A meeting names several. Check and gate nodes name none. |
| `prompt` | The brief. A path to a prompt file or a brief recipe (`src/airlie/prompts/briefs/*.yaml`), with the inputs available to the template. A conversation has an opening prompt and a turn prompt. |
| `inputs` | Named artifacts from earlier nodes, plus loose files: repo paths, the story card, the run directory. Each input is a name and the format the node expects. |
| `outputs` | What the node produces. Each output is a name, a format and where it lands (see Artifacts). |
| `steps` | The step count. What it bounds depends on the type: parse retries for an ask, turns for a conversation or meeting, tool calls for a write, retries for a check. |
| `tools` (my call) | The tools the role gets in this node. Today this varies per box for the same role, so it belongs on the node, not the role. Defaults to the role's tools. |
| `done` (my call) | What ends the node early. A schema validating, a verdict value, a check passing. Defaults per type. |
| `on_fail` (my call) | What happens when the step count runs out: `block`, `continue` (an output that may be empty, as the survey does today), or `loop` to a named node with the failure as an input. |

## The node types

Seven types. Five are built in this round; two are named for later.

### ask

One seat, read-only tools, one prompt rendered from the inputs, one reply parsed against the output
format. The step count is the number of tries until the reply parses (today `stage_attempts`, 2, via
`Asker.usable` at `src/airlie/story/asking.py:389`). The model never sees its earlier failed tries.

Today's boxes that are asks: survey, refine, plan layers (each round is one ask; rounds are a loop
wire), size review, package ask, package critic, proof revision, criteria revision, skill pick, the
reviewer's review, the supervisor's reading when the final gate fails, and Muse's reading at a block.

### conversation

One seat with a held transcript. An opening prompt starts it; a turn prompt is sent each turn with that
turn's inputs; each turn yields one artifact. The step count is the maximum number of turns. The
conversation ends early when `done` says so (today: the first unusable draft or a proof change). Today's
implementation is the held conversation at `asking.py:258`.

Today's boxes that are conversations: draft packages (one turn per layer). The initiative's imagine
step and the epic plan are conversations too, for later.

### write

One seat with write tools in a worktree. The model drives: it reads, edits and runs tools until it
says it is done or hits the step count, which caps its tool calls. This is the ReAct loop the coder runs
in the studio today (`studio.py:851` onward). It is a type, not a conversation, because the
orchestrator does not drive the turns.

Outputs: a branch ref with the commits, the model's own account of what it did, and a session (see
Artifacts). The worktree is an input, made by a check node, never created inside write.

A write node may carry an **advisor**: the supervisor's in-loop triggers (`repeat_guard`,
`red_streak`) fire inside the ReAct loop today (`src/airlie/teams/native_team.py:269, 335`) and their
advice is spliced into the coder's next message. That is inside the protocol, so it cannot be a wire.
`advisor: supervisor` on the node names it; the node runs it as the team loop does today.

Today's boxes that are writes: the layer build's coder loop, the coder's done-check (a turn on the same
session), the coder's `gate-failed` follow-up (another turn), the design role drawing a mockup page, and
the architect writing a package with tools (`package_tools`).

### meeting

Several seats and the model-less checker (`CHECKER = "airlie"`, `src/airlie/story/amigos.py:515`)
in a round-robin on one shared transcript. The step count is the maximum number of turns
(`amigos.max_turns`, 12). At the cap, each part's owner decides. Output: the agreement. The state is
saved every turn so a killed meeting resumes. Today's implementation is `Meeting`
(`amigos.py:948`).

Today's boxes that are meetings: three amigos. The initiative's consult rounds are a meeting with
read-only asks as turns, for later.

### check

No seat. A Python callable or a shell command with the inputs, giving pass or fail and a report. The
step count is retries. Checks make and change worktrees, run git, run gates and publish.

Today's boxes that are checks: prepare GitHub, intake, base, gate, covered-below, the satisfied check,
package readiness (`PackageChecker`), place, the final gate, squash, publish, the mockup page check,
the epic budget check, and the diagnosis evidence pack at a block.

### gate

No seat. The node posts something for a person (a card status, a comment, a pick) and **ends the
run**. It does not hold the process or the lane. The line's bookkeeping sees the person's action later
(a board status, a comment, a pick) and resumes the workflow at the node after the gate. The gate's
output is what the person did. It has no step count. Today's human gates: the initiative question and
answer, the mockup pick, the hours approval, and merge, which is the review status at the end of a
story. A check that must wait on something outside the story, as `base` does on a dependency not yet
in review (`StoryWaiting`), ends the run the same way.

### team and pair (named, not built)

- **team**: k copies of one seat, each with the same prompt and no view of the others, then a reducer
  node that picks or merges. Different from a meeting because nobody sees anybody.
- **pair**: two seats on one shared transcript with a turn rule that is not round-robin: driver and
  navigator, or proposer and judge.

Both fit the rule. Neither is needed for the story pipeline.

## Artifacts

An artifact is a named, typed value a node writes and later nodes read.

- **Store.** The story's state (`state.json` in the story directory) is the artifact store. Artifact
  names are the fields that exist today: `base`, `gate`, `survey`, `story`, `layers`, `agreement`,
  `drafts`, `built`, `satisfied`, `prs`, and so on. Keeping the names keeps `_cleared` in
  `src/airlie/story/restart.py` and the screen's event readers working unchanged.
- **Formats.** `json:<Model>` for a pydantic model, `markdown`, `ref:branch`, `ref:worktree`,
  `ref:pr`, `file`. A ref artifact holds the path or name, not the contents.
- **Big things stay on disk** where they are today: `meeting.md`, `meetings/<n>.md`, `pr*.md`,
  scratch dirs and worktrees. The artifact is the ref.
- **Checking before running.** The composer reads a node's `inputs` and refuses to start the node if
  a named artifact is missing or of the wrong format. This is the check that makes assembly cheap:
  a mis-wired workflow fails before any model runs, not three hours in.
- **Loop inputs.** A loop-back wire carries an artifact (the reviewer's findings, the gate's tail,
  the coder's account) into the target node as a named input, as `_replan_feedback` does today.

### Sessions: a conversation that crosses nodes

Dissolving the build (D3) and running the same prompts (goal 1) only both hold if a model's live
conversation can continue from one node into the next. Today it does, inside one method: the
done-check, the review fix rounds and the gate follow-up are all continuations of the coder's one
conversation (`StudioOrchestrator._context(..., parent=run.result)`, `studio.py:549, 584, 645`), and a
package retry inside a layer is a turn of the architect's drafting conversation (`_resumed`,
`pipeline.py:1950`). A cold restart of that conversation cost 47 minutes once, because the prefix
cache is lost.

So there is one more artifact format, **`session`**, and it is the only run-scoped one:

- A `write` or `conversation` node outputs a session alongside its other outputs.
- A later node of the same type names that session as an input and continues it: same transcript,
  same tools, same worktree, one more turn. The node's prompt is that turn's nudge.
- A session lives in the process. It is never written to `state.json`. If the run dies, the next node
  that names it starts a fresh one from the persisted artifacts, which is what `_resumed` does today.
- Continuing a session is config, not a type: the node names an input. Which model sees what is still
  fixed by the type.

This changes two placements from the first draft: the coder's **done-check is a `write` turn on the
coder's session**, not an ask, because it runs the coder with write tools and can change the tree
(`done_check_done` logs `changed=`). And the layer's **`package` node continues the `drafts` session**
when it retries, so the `drafts` conversation outputs a session that the layer bundle names as an input.

## Workflows

A workflow is a YAML file. One per workflow, under `config/workflows/`, pulled in by `airlie.yaml`'s
`includes:` like the other fragments. Role prompts and brief paths resolve against the root file's
directory, as today.

**Wiring moves**, and where the story pipeline uses each:

| Move | Meaning | Used by |
|---|---|---|
| sequence | the default: next node after this one | nearly everything |
| parallel | run these nodes at the same time, join after | survey with refine |
| loop | send an output back to an earlier node, at most N times, until `done` | plan rounds, review fix rounds, gate retry, layer replan, proof revisions |
| branch | pick the next node by an output value | base (stacked, pinned, trunk); package (proof change, satisfied, ready); publish (single, stack, none) |
| for each | run a sub-workflow once per item of a list artifact | the layer lane, once per layer |

A sixth move, **on event** (run a node when another node logs an event), was in the first draft for the
supervisor. It is not needed: the supervisor's gate trigger is a plain sequence and branch, and its
in-loop triggers live inside the write node (see the write type and the supervisor below). It can come
back when a workflow needs it.

**Bundles.** A workflow file can name another workflow file as one node. The layer lane is the first
bundle: `layer.yaml` holds package, checks, place, write, done-check, review, gate, squash, and the
story workflow runs it for each layer. Bundles are how the reuse Noah wants arrives: not more types,
but wired groups a new workflow drops in whole.

**Exits.** `block`, `wait` and `cancel` stay as they are: a node's `on_fail: block` or a raised
`StoryWaiting` ends the workflow and the existing exit code (diagnose, fix card, back to intake) runs.
The exits are not nodes in this round.

## The story workflow as nodes

The box list from `2026-11-02-pipeline-matches-diagram-design.md` is the reference. Each box becomes
these nodes. Nothing a box does changes.

**Plan lane** (`story.yaml`, runs to the `planned` milestone):

| Box today | Node(s) | Type | Notes |
|---|---|---|---|
| prepare GitHub | `prepare` | check | build half only |
| intake | `intake` | check | |
| base | `base` | check | branch: stacked, pinned, trunk; `StoryWaiting` exits |
| gate | `gate` | check | |
| survey | `survey` | ask | parallel with refine; `on_fail: continue` |
| refine | `refine` | ask | skipped for epic story cards |
| plan layers | `layers` | ask | loop over rounds; `size_review` ask on the branch where a layer is too big, "split" loops back |
| skill pick | `skill` | ask | the Kev router is a role |
| three amigos | `amigos` | meeting | off when `amigos.enabled` is false |
| planned | milestone | event only | the lane hands over here |

**Build lane:**

| Box today | Node(s) | Type | Notes |
|---|---|---|---|
| draft packages | `drafts` | conversation | one turn per layer; after each turn, `critic` ask with one revision loop |
| per layer | `layer.yaml` for each layer | bundle | below |
| publish | `publish` | check | branch on `story.publish` |
| review | `review` | gate | the human merge |

**The layer bundle** (`layer.yaml`):

| Box today | Node(s) | Type | Notes |
|---|---|---|---|
| covered below | `covered` | check | branch: satisfied path skips the rest |
| package | `package` | ask (or write with `package_tools`) | takes the draft first; `critic` ask after; loop up to `package_attempts` |
| proof change | `revise_proof` ask, `revise_criteria` ask | ask | the branch that does not cost a try |
| satisfied | `unproven` | check | |
| checks | `readiness` | check | `PackageChecker`; lint-only failures loop once for free |
| place | `place` | check | makes the worktree, writes and locks the tests; outputs `ref:worktree` |
| build: coder | `write` | write | the coder with the `layer-build.yaml` recipe; `advisor: supervisor`; outputs a session |
| build: done-check | `done_check` | write | one turn on the coder's session: the coder is handed its diff and ticks each criterion; may change the tree |
| build: reviewer | `review` | ask | the reviewer seat; blocking findings loop to `fix`, up to `review.rounds` |
| build: fix round | `fix` | write | one turn on the coder's session with the findings |
| build: final gate | `final_gate` | check | red branches to `gate_advice` |
| build: gate advice | `gate_advice` | ask | the supervisor seat reads the tail; branch: a wrong-test verdict goes to `package`; otherwise `follow_up` |
| build: follow-up | `follow_up` | write | one turn on the coder's session with the tail and the advice; then `final_gate` once more |
| outcome | wire | loop | nothing changed or red: loop to `package` with the account and tail, up to `layer_attempts`; last attempt blocks |
| squash | `squash` | check | provenance trailers |

The `package` node names the `drafts` session as an input, so a retry is a turn of the architect's
drafting conversation, as today.

**The supervisor** is two things, because today it is two things (`src/airlie/supervisor/layer.py`):

- At the final gate (`gate_retry`): an ordinary **ask node** with a branch, shown above. Its advice is a
  named output that `follow_up` takes as an input, which is what `with_advice` does today
  (`studio.py:1000-1019`). The wrong-test short-circuit is the branch.
- Inside the coder's loop (`repeat_guard`, `red_streak`): the **advisor on the write node**. It cannot
  be a wire because it fires between the model's own tool calls.

Both write the same ledger rows and settle the same outcomes as today. The per-layer cap of six
(`LayerSupervisor`) is state the advisor and the ask share through the ledger. The other triggers at
`ledger.py:22` (`precode_retry`, `static_retry`, `amend`, `replan`) fire from nothing today and get no
node; `block` stays in the exits.

## Running the same

"The same" means, for a story run on the composer and on the driver:

- the same `events.jsonl` kinds and fields, in the same order, so the screen lights the same boxes
  and the now bar reads the same;
- the same `state.json` fields with the same values at each milestone, so restart-from-step's
  `_cleared` and `replay_layers` work unchanged;
- the same prompts sent to the engine, checked by diffing the traces of a driver run and a composer
  run of the same story on the bench;
- the same supervisor ledger rows.

The composer logs one extra event per node, `node_started` and `node_finished`, with the node name
and type. The screen ignores events it does not read, so this is additive. One reader does not:
`_handoff` (`pipeline.py:731`) treats any event after `story_planned` other than `story_start` and
`story_done` as a resume and posts a "resumed" note. It must ignore the node events.

## Migration

- `story.composer: false` in `airlie.yaml` keeps the driver. The bench config turns it on.
- The driver stays until three clean roman runs on the composer match the driver's traces. Then the
  flag flips to true on main, and the driver is deleted in a later PR once a week of stories has run.
- The node runners are adapters over the existing functions in `pipeline.py`, `asking.py`,
  `amigos.py`, `studio.py` and `package_checks.py`. The prompts, context building, the team loop, the
  reviewer ask and the gate are not rewritten. Once the composer is the only driver, the `_methods` in
  `pipeline.py` get folded into the runners and `pipeline.py` shrinks to the exits.
- **The studio split is in scope.** `StudioOrchestrator.execute_task` (`studio.py:851`) does the
  worktree, place, map, coder, done-check, review rounds, gate and follow-up in one call. Dissolving
  the build (D3) means splitting it into separately callable pieces that share a session and a
  worktree ref: the run, the lock, the gate memo and `scope_base` become session state. The CLI, serve
  and `Asker` call `execute_task` today and keep a wrapper that runs the pieces in order. This is the
  riskiest step of the round. It is done after the plan lane is clean on the composer, so the bench
  shows a regression in the split and not in the composer.

## Decisions

- **D1.** A type is a protocol; a node is config; a workflow is wiring. (Noah)
- **D2.** Seven types: ask, conversation, write, meeting, check, gate; team and pair for later. (Noah
  and me)
- **D3.** The layer build is dissolved into write, done-check, review, gate and squash nodes. (Noah)
- **D4.** Loops are wires with a bound, not nodes. A review-and-loop is a bundle, not a type. (me,
  agreed in chat)
- **D5.** Bundles: a workflow file can be used as one node of another. (me)
- **D6.** Finish line: the story pipeline rebuilt from nodes and running the same. No second workflow.
  (Noah)
- **D7.** Workflow files are YAML under `config/workflows/`. (me)
- **D8.** The story state is the artifact store, with today's field names. (me)
- **D9.** The same events and state come out; the web app does not change. (me)
- **D10.** Behind `story.composer`, flipped after clean bench runs. (me)
- **D11.** The worktree is an artifact made by `place`, named as an input by every node that touches
  it. (me)
- **D12.** The supervisor is an ask node with a branch at the final gate, and an advisor carried by the
  write node inside the coder's loop. No on-event wiring this round. (me, after the builder check)
- **D13.** A `session` artifact lets a write or conversation node continue in a later node of the same
  type. It is run-scoped and never persisted. The done-check, fix rounds and gate follow-up are write
  turns on the coder's session; a package retry is a turn on the drafting session. (me, forced by D3
  and goal 1 together; **the one call to check**)
- **D14.** A gate node ends the run; the line resumes the workflow after the gate when the person
  acts. `StoryWaiting` is the same mechanism from a check. (me)
- **D15.** The studio split is in scope for this round, done after the plan lane is clean on the
  composer. (me)

## Left to the builder

- The composer's shape: a small interpreter over the YAML, or a Python DSL the YAML loads into.
  Either is fine as long as the YAML is the source of truth.
- How a node's `prompt` template reaches its inputs (the brief recipe mechanism from
  `2026-11-01-brief-recipes-design.md` is the obvious fit).
- The exact schema of the workflow YAML, as long as it holds the contract fields and the five wiring
  moves.
- How a session is handed between nodes in the process: an object on the run, a registry keyed by
  name, or the composer passing it. Any of these, as long as `state.json` never holds it.
- How `for each` joins: sequentially per layer today; the design does not require parallel layers.
- Order of work. A sensible order: the contract and composer with tests on toy workflows; the check
  and gate nodes; the ask node and the plan lane on the bench; the conversation node and drafts; the
  layer bundle with the build dissolved; the supervisor on events; the trace diff and the flip.
- Where `node_started` and `node_finished` go in the hover cards, if anywhere. Nothing is required.
