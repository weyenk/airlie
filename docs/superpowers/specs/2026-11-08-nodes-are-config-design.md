# Nodes are config

A node is configuration a person fills in: type, role, prompt, inputs, outputs, steps. The code behind a
node is the type's protocol and nothing else. This redesign removes the Python hooks the builder exposed as
dropdowns, makes parallel work a matter of drawing lines, and adds the team node. It goes ahead of generic
nodes Part 2 (`2026-11-06-generic-nodes-design.md`, Migration), whose studio split still stands.

Readable copy for Noah: https://claude.ai/artifact/GCHvrzmFTbUxrA8xwVeM9A

## What went wrong

Noah, 2026-11-08, on the shipped builder: "the run dropdown is not at all what i was thinking. it feels like
you locked a bunch of functionality in code." He is right. Measured on the shipped workflows:

| Workflow | Nodes | Of them `check` (a Python function by id) | Model nodes | Python hooks the model nodes name |
|---|---|---|---|---|
| story | 19 | 12 | 5 (2 ask, 1 conversation, 1 meeting, 1 each) | prompt, parse, turn, after, when, done |
| layer | 5 | 2 | 3 (1 conversation, 1 ask, 1 write) | prompt, run, when |

The node form shows `run`, `parse`, `turn`, `after`, `when` and `done` as dropdowns of registry ids, and
`prompt` as a dropdown of Python brief builders beside the prompt files. Changing a node means picking a
different function. The founding rule (D1 of the generic nodes design: a type is a protocol, a node is
config, a workflow is wiring) was kept in the design and broken in the build for the model nodes, because
Part 1 was built as a strangler over `pipeline.py` and the builder then surfaced the strangler's seams.

## The node, by field

Every model node (ask, conversation, write, meeting, team) is these fields and nothing else a person has to
know code for:

| Field | What a person fills in | Notes |
|---|---|---|
| `type` | one of ask, conversation, write, meeting, team, check, gate | the protocol |
| `role` | a seat from `config/roles.yaml`; a meeting names several; a team names a supervisor and a worker | |
| `prompt` | text, written in a text area; it names inputs by `{name}` | a conversation also has a `turn` text; a team has a `worker` text |
| `inputs` | artifacts earlier nodes produce, plus loose ones (`card`, `repo`), picked from a list | a line drawn onto the box adds one |
| `outputs` | name, format, and for a structured answer a short field list | see Outputs |
| `steps` | a count; what it bounds depends on the type (parse retries, turns, tool calls) | a config path such as `story.package_steps` stays allowed |
| `tools` | the tools the role gets in this node | defaults to the role's |
| `on_fail` | block, continue, or loop to a node with a bound | |

Gone from the form and from the schema for model nodes: `run`, `parse`, `turn` as an id, `after`, `when`,
`done`, `args`. Their jobs move:

- `prompt` and `turn` ids → prompt text on the node (or a `prompt:<id>` file, D19 of the builder design).
- `parse` → the output's format and fields (Outputs).
- `after` (a board comment, an event) → a check node drawn after the model node, from the catalog.
- `when` (skip the node) → a condition on the line that leads to it (The canvas).
- `done` (end an ask early) → the output's fields: an ask ends when its output parses.
- `args.turns`, `args.failed` → the conversation's `turn` text and `on_fail`.

The existing `story.*` brief builders, parsers, predicates and afters are deleted, not kept as options.

## Outputs

An output is a name, a format and, for a structured answer, a short field list:

```yaml
outputs:
  verdict:
    format: json
    fields: {decision: "agree | object", reasons: "list of text"}
  notes: markdown
```

The parse is generic: the runner asks the seat for a fenced block of the format, checks the named fields are
present with the kinds given, and asks again with the reason up to `steps` times. A format without fields
takes the whole reply (`markdown`, `text`). A `json` output with fields is stored as a dict and read by later
nodes as `{verdict.decision}` in a prompt or `verdict.decision` on a condition. The list of kinds a field
may name is small: text, number, bool, list of X, one of a | b. Noah's call: a short field list, not a JSON
schema.

The artifact store stays as Part 1 built it: a declared output lands in the story state under its name,
dotted names per item, and unknown names in the generic artifact store (D15).

## Checks

A check is deterministic work: fetch the trunk, place files, run the gate, publish, squash, comment on the
card. That is code by nature and the one honest exception. Checks become a catalog of named operations,
each with its own declared inputs and outputs, shown in the builder as "what this check does", with its
inputs and outputs filled in, never as a function picker on a generic form:

| Operation | Inputs | Outputs |
|---|---|---|
| intake | card | story (the card as a story) |
| base | card, repo | base (ref:branch) |
| gate | repo, base | gate (json) |
| place | package, base | attempt (json), studio (session) |
| run tests | worktree | result (json) |
| publish | built, base | prs (ref:pr) |
| squash | attempt, built | built.{item} |
| comment on card | card, text | — |
| mark planned / built | — | planned, built |

New checks are still code, by design: a Python function registered under a name with a declared signature,
and the catalog reads the signature. The table above is the migration of today's twelve; the layer's build
checks come with Part 2's split.

## The canvas

Boxes are nodes. Lines are artifacts. Nothing else is drawn.

- A node shows one named port per output. A line is dragged from a port to a box: it says "this artifact
  goes to that node" on the canvas itself, and dropping it adds the artifact to the target's inputs.
- Two lines out of one node mean both targets run at once. There is no `parallel` box.
- A node with two lines coming in waits for everything they carry, because a node starts only when its inputs
  exist. There is no join box. Noah, 2026-11-08: wait-for-both only; no racing lines.
- A condition is a label on a line (as the branch wires are today): the line is taken when the named output
  has the value. A loop is a line drawn back to an earlier node, with its bound and what it carries on the
  label.
- A line that carries a list into a bundle runs the bundle once per item. That replaces the `each` box.
- Layout stays in the YAML's `layout` block; the YAML stays the source of truth, and a drag changes only
  layout.

In the schema, this is a wiring list per node instead of `next`, `branch`, `loop`, `nodes`, `over`:

```yaml
survey:
  outputs: {survey: markdown}
  to: [plan_layers]                      # the artifact goes there; drawn as a line from the survey port
plan_layers:
  inputs: [story, survey]                # waits for both lines
review:
  to:
    - {node: fix, when: "verdict.decision == object", loop: {max: review.rounds}}
    - {node: gate}
build_layers:
  inputs: [layers]
  bundle: layer                          # a list carried into a bundle: once per item
```

## Editing

- The prompt is a text area, never a dropdown. The inputs picked for the node are listed beside it so a
  name can be dropped in as `{name}`.
- The permanent right-hand fields panel goes. Editing a box opens a large modal, most of the window, with
  the prompt taking the bulk of it. Wires open the same modal with their own fields. On a phone the modal is
  the bottom sheet the app already uses.
- My call, changeable: one click selects a box and shows a small card with its name and type; double-click,
  or Edit on that card, opens the modal.
- With the panel gone the canvas has the full width, which is what the wide story graph needed.

## The team node

Noah, 2026-11-08: "a supervisor node that can dynamically spin up N workers. The supervisor delegates
chunks of work, the workers complete the chunk and hand it back to the supervisor. The supervisor assembles
the chunks while it waits for each worker to complete." This is the `team` type the generic nodes design
named and left for later. It is a type because the supervisor decides the fan-out at run time, which no
wiring can express in advance.

Configuration, no code:

| Field | Meaning |
|---|---|
| `role` | `{supervisor: architect, worker: coder}` |
| `prompt` | the supervisor's brief: what to split and how |
| `worker` | the worker's template; receives one assignment plus the node's inputs |
| `inputs` | what the supervisor needs to plan the split |
| `outputs` | `results` (list of the workers' hand-backs) and the supervisor's synthesis under a name later nodes read |
| `steps` | per worker |
| `workers` | the cap; default 5 |
| `tools` | the workers' tools: write tools give each worker a worktree and a diff to hand back; read tools give an answer |

Protocol, fixed in code: the supervisor replies with up to `workers` assignments; the workers run in parallel
on the engine, each seeing only its chunk; as each hands back, the supervisor gets a turn to fold the result
into its running synthesis; when the last is in, it writes the final output. Workers talk only to the
supervisor, only by handing back. One node, not two: the worker seat's tools decide reading or writing, and
a workflow that needs both has two team nodes.

From Cohere's "Lessons from scaling teams of frontier coding agents" (read 2026-11-08): the lead must not
block (polling for finished workers cut wall-clock about a fifth at the same quality); a worker sees only its
subtask; five workers was the useful ceiling, implementation teams peaked at four; worker-to-worker
messaging failed and weaker leads were swamped by it; delegating exploration and planning alone matched
delegating everything at half the cost; their workers shared one workspace and the lead integrated by hand,
so per-worker worktrees with the supervisor reading diffs is the same idea with less chance of two workers
trampling one file.

The pipeline's per-layer supervisor (Muse as the coder's advisor) is not this: it reacts to a stuck coder and
stays the advisor on the write node.

## The story workflow rewritten

The story runs on this surface, the same events and state coming out (the Part 1 side-by-side tests are the
contract):

| Today | Becomes |
|---|---|
| `survey` ask with `prompt: story.survey`, `after: story.surveyed` | ask: role architect, prompt text from the survey brief, output `survey: markdown`; the board note is a `comment on card` check after it |
| `refine` ask with `parse: story.story`, `after: story.refined` | ask: role product, output `story: json` with the story's fields; comment check after |
| `plan_layers` conversation with `args.turns: story.layers_turns`, `parse: story.layer_plan` | conversation: turn text, output `layers: json` with the plan's fields |
| `amigos` meeting with `run: story.amigos`, `when: story.amigos_on` | meeting: roles product/architect/tester, prompt + turn texts, output `agreement: json`; the `when` becomes a condition on the line into it |
| `parallel` box over survey and refine | two lines out of `gate` |
| `each` box over layers | a line carrying `layers` into the `layer` bundle |
| `drafts` conversation with `args.turns`, its critic inside the turn body | conversation: turn text, output `drafts_session`; `critic` ask with one loop line back (D19) |
| 12 checks by `run:` id | the same 12 from the catalog, with inputs and outputs declared |
| the layer's `package` ask and `build` write by `run:` id | package: ask with the package brief as text and `package: json` fields; build: Part 2's split |

The briefs' text moves out of Python into the prompt files the builder already reads (`prompt:<id>`, hub
wins). Where a brief today assembles files and sections by code (the layer-build recipe), the recipe becomes
an input-rendering rule of the prompt file, not a Python function. Nothing a brief says to a model changes
in this round; the trace diff (Part 2, Task 6) proves it.

## Bundles and isolation

Noah, 2026-11-08: the story calling the layer bundle is the right way to manage complexity, and it makes
workflows a first-class thing to manage; steps are isolated and share artifacts only through inputs and
outputs.

- **A bundle is a step.** A workflow declares `inputs` and `outputs` as a node does; dropped on a canvas it
  is a box with those ports, and the composer checks the lines into it the same way. Nothing crosses the
  boundary by name: the layer bundle's `loose: [card, story, layers, base, gate]` becomes its declared inputs,
  and the parent's line into it carries the list it runs over.
- **Open in place.** Double-click on a bundle box opens that workflow in the builder with a breadcrumb back.
- **Use tracking.** The Workflows page shows where a bundle is used; deleting or renaming one in use is
  refused with the list.
- **Snapshots stay, with a second door.** A running card keeps the snapshot it started with. A card gets the
  action "use the current workflow": the snapshot is replaced with what the files and drafts say now and
  the run continues by the artifacts present, so a card that holds `story` and `layers` goes on from there
  under the new wiring and a node whose outputs are gone runs again. This is how a person works the kinks
  out of a new workflow without restarting every card. The restart-from-step keeps the old snapshot, as
  today.
- **Shared conversations.** Steps share only through inputs and outputs. The one shared thing today is a
  `session` artifact, a model's live conversation continued by a later node of the same role (the coder's
  done-check, fix and follow-up; the architect's package retry), kept because a cold restart of the coder's
  conversation once cost 47 minutes of lost cache. Noah's rule: allowed only when the same role does both.
  Proposed, pending Noah: a session line is valid only between two nodes of the same role, enforced by
  `check_workflows` and refused in the builder; or sessions go for now and each such turn starts fresh.

## Decisions

- **D1.** A model node is pure configuration: type, role, prompt text, inputs, outputs with fields, steps,
  tools, on_fail. No registry ids on model nodes. (Noah)
- **D2.** Checks are a catalog of named operations with declared inputs and outputs; new checks are code.
  (me, the one exception)
- **D3.** Outputs are name + format + a short field list; the parse is generic. (Noah: not a JSON schema)
- **D4.** Lines are artifacts; parallel is drawing two lines; join is inputs; no parallel, join or each box.
  (Noah)
- **D5.** Wait-for-both only; no racing lines for now. (Noah)
- **D6.** A condition and a loop are labels on a line. (me, following today's branch wires)
- **D7.** The prompt is a text area. (Noah)
- **D8.** The fields panel becomes a large modal; click selects, double-click or Edit opens. (Noah; the
  opening gesture is my call)
- **D9.** A team node: supervisor + N workers, assemble as each returns, cap 5, workers report upward only;
  one node, the worker's tools decide reading or writing. (Noah; the cap and the upward-only rule from the
  Cohere lessons)
- **D10.** The story workflow is rewritten on this surface and the Python brief builders and parsers are
  deleted. (me)
- **D11.** Part 2's studio split still follows; this round goes first. (me)
- **D23.** A bundle is a step with declared inputs and outputs; nothing crosses its boundary by name. (Noah)
- **D24.** A card can adopt the current workflow in place of its snapshot and continue by the artifacts present. (Noah)
- **D25.** Steps share only through inputs and outputs; a shared conversation only between nodes of the same role, if kept at all. (Noah; whether sessions stay is pending)

## Left to the builder

- The exact grammar of `{name}` and `{name.field}` in prompt text, and how a list input renders.
- How the catalog reads a check function's signature (type hints, or a small declaration).
- The modal's layout; the phone sheet.
- Whether the YAML keeps `next` as a shorthand for a single unconditional line.

## Answers from the builder check

A reviewer read the design as the engineer who would plan from it (2026-11-08) and asked eleven questions.
None needs Noah; each is settled here as my call.

- **D12. The driver and the flag.** The driver stays until Part 2's flip. The Python briefs, parsers and
  afters leave the registry and the YAML in this round; their copies inside `pipeline.py` go with the driver.
  The side-by-side tests (driver against composer) are what proves the prompt files say the same words.
- **D13. Computed inputs.** A value a brief computes from the repository or the state (the repository guide
  at the base, recent lessons, the dossier, a layer's sources, the agreed proof) is a catalog check with
  that artifact as its output, drawn before the node that reads it. A value from config (`max_layers`, a
  budget) is readable in prompt text and conditions as `{config.story.max_layers}`. Nothing else reaches a
  prompt.
- **D14. Same events and state.** The contract stays Part 1's: the exact event stream and the same
  `StoryState` fields, so the web app does not change. A model node may declare `events: {done: survey_done,
  failed: survey_failed}` to name the legacy kinds its outcomes emit; the phase markers
  (`finish("refine")` and the like) are the catalog's mark checks. Where no legacy kind is named only the
  node events are emitted.
- **D15. Alternatives that join.** Two nodes may produce the same artifact when their lines carry exclusive
  conditions (refine or take-card-as-story; amigos or agree-plain). A node's input is satisfied when the
  artifact exists, or when every line that could carry it was not taken. A line not taken counts as done;
  nothing waits on a skipped branch.
- **D16. What a condition reads.** A condition is a small expression over outputs (`verdict.decision ==
  object`, `survey exists`), loose inputs (`card.level == story`) and config (`config.amigos.enabled`), with
  `not`, `and`, `or`. A predicate that needs code (per-item built or satisfied sets) is a catalog check with
  a bool output, and the condition reads it.
- **D17. Lines that carry nothing.** A line may be drawn from a box's edge rather than a port: it orders
  without carrying. A check with no output is skipped on resume when its completion is recorded; the state
  keeps a `done` set of output-less node names for that.
- **D18. The catalog is complete.** Every check the story and layer workflows name today is in it
  (take-card-as-story, the four costed marks, agree-plain, finish, covered included); the table in Checks is
  the shape, not the list. Place, run tests and squash arrive with Part 2.
- **D19. Logic beyond field kinds.** Validation against the story (ids, `max_layers`, renumbering) is a
  catalog check after the ask with a loop line back carrying the reason. A conversation ends when its output
  parses, within `steps` turns; the size review is an ask with a line back into the planning conversation;
  the drafting critic is an ask with one loop in the layer bundle (as the generic nodes design's table
  already had it). The rewrite table gains `drafts` and `critic` rows.
- **D20. The meeting's protocol stays code.** Turn order, team-state resume, the proof plan and the
  agreement's effect on `story` and `layers` are the meeting type's protocol; prompt and turn texts are
  config and `agreement`, `story`, `layers` are declared outputs.
- **D21. Old snapshots and drafts.** A card in flight finishes on its snapshot through an old-schema reader
  kept for one release. A board draft in the old schema is migrated once at load: wiring becomes lines; a
  node that named a code id is marked with a problem ("write this node's prompt") and stays unrunnable until
  a person fills it in.
- **D22. Writing workers.** A writing worker hands back a diff and its branch. The supervisor, one held
  conversation across hand-backs, applies each diff to its own worktree in arrival order; a diff that does
  not apply comes back to it as a failed hand-back to reassign. The node's outputs are `results`, the
  synthesis, and a `ref:branch` of the assembled work. Assignments are one fixed JSON shape
  (`{assignments: [{id, task, files}]}`), parsed as any output is.

## Order of work and the finish line

1. Schema and composer: outputs with fields and the generic parse; `to` wiring with conditions, loops and
   per-item bundles; the model-node fields without registry ids; the catalog of checks.
2. The story workflow rewritten, Python briefs and parsers deleted, side-by-side tests green.
3. The team node runner and its tests with the fake engine.
4. The builder: ports and lines, the modal, the prompt text area, the catalog view.
5. Then Part 2's studio split on the new surface.

Finish line: a person builds a new workflow in the builder from the palette, writing prompts, picking inputs
and declaring outputs, with no dropdown that names a Python function; and the story runs on the rewritten
file with the same events and state as today.

## As built (plan 1, 2026-11-08)

What differs from the text above, and what was added, in plain words.

- **Bundles.** A `bundle` node type with `workflow` and `each` replaces the design's `bundle:` key; the
  design's `each` box is gone.
- **Conditions.** `expr := term (("and"|"or") term)*; term := "not" term | "(" expr ")" | atom;
  atom := path "exists" | path ("=="|"!=") literal | path`. Literals are a quoted string, a number, `true`,
  `false` or a bare word. `and` binds tighter than `or`. A missing path is false. A bool equals only a bool
  (`true` never matches 1). No part of a path, here or in a placeholder, may start with `_`, and a path
  descends only through mapping keys, pydantic fields and dataclass fields, never an attribute.
- **Field kinds.** `text | number | bool | list of <kind> | one of a | b`, optional with a trailing `?` on the
  outermost kind.
- **Prompt placeholders.** `{name}`, `{name.field}`, `{config.a.b}`, `{feedback}`, `{item}`, optional blocks
  `{?name}...{/name}`, and literal braces `{{ }}`. A prompt may name only the node's own inputs.
- **The done set.** `StoryState.nodes_done` (omitted from the file when empty): it skips only output-less
  nodes; a node with outputs is skipped only by its outputs. A restart drops every entry.
- **Transient outputs.** `OutputSpec.transient` and `Check.transient` mark computed inputs and condition
  booleans as run memory, recomputed every run. `Check.every_run` marks (intake, `story.costed` (the
  `costed_*` nodes), planned, build_done, layer_start) never enter the done set.
- **When a node runs.** An input is satisfied when its artifact is present or its producer is done; a line
  fires when its source finishes; a dead line counts as done; a `Stalled` error names what a run waits for.
- **Loops.** A loop line re-runs its target, everything downstream and every node that ran and reads an
  output of those (transitively, line or no line), and clears their outputs, also inside bundles (done
  names are scoped `<bundle>/<node>@<item>`); a name a finished node outside the loop also writes keeps its
  latest value. A blocking node's loop line must go back to a node upstream of it.
- **The `output-format` rule.** The hub rule is never auto-appended to an ask; it exists for the builder and
  for the team's two rules, `team-assign` and `team-handback`.
- **Events.** Legacy events declared per node (`events: {done, failed}`) carry exactly the driver's fields.
- **The story this round (option A).** `survey` is a generic ask on `prompt:story-survey`; `refine`,
  `plan_layers`, `draft_packages`, `package` and `build` stay named catalog operations until Part 2, because
  their round-and-retry protocols are code whose exact events the equivalence tests protect. The `plan_check`,
  `size_review` and `critic` nodes and five prompt files move to Part 2. New nodes: `to_build`, `story_card`,
  `request`, and a no-op layer `start`.
- **The upgrade.** Old wiring becomes lines (a `*` branch default becomes `not (<on> == a or ...)`); parallel
  children inherit the parent's lines (their own wiring is a problem); `each` becomes a bundle; `loose`
  becomes inputs. A node that named code is marked "write this node's prompt" and is unrunnable. Drafts
  upgrade where they are read.
- **Adopting the current workflow.** A changed node (its definition, or the text of a draft prompt file it
  names) is treated like a loop target (cleared with its downstream); one that runs a stage-gated operation
  (refine, the layer split, the meeting, the build's end, the publish) also clears the stages from that
  point on, as a restart from there does. The card is held meanwhile; the note, posted first, names every
  node that will run again.
- **Checks.** A check declares the `args` its function requires (`story.costed`: `stages`); rule 9 holds a
  node to them and to each output's declared format. A bundle workflow's nodes write only its `outputs`
  (memory heads and sessions aside). A meeting runs the amigos protocol only: seats product, architect,
  tester, reading story and layers (the catalog route's `meetings`).
- **The team node.** Readers or writers, by the worker's tools; writers hand back diffs the supervisor applies
  with `git apply --3way`; the cap is 5 (at least 1); diffs are capped for the supervisor; a worker's prompt may name
  `{assignment}`.
- **The builder.** The live Vue builder cannot open the v2 files until plan 2 (unknown type `bundle`); its
  tests read frozen v1 copies; plans 1 and 2 merge together.

## As built (plan 2, 2026-11-09)

The Vue builder reads and writes the v2 files. What differs from the design text above:

- **Ports.** A box has a port per output, and one edge port for lines that only order. A line from an output
  port carries that artifact; artifacts are encoded as the target's inputs (a `to` entry is node, `when`, `loop`
  and nothing else). A bundle box's ports are its bundle's declared outputs, read (with its inputs) when the
  canvas loads and when the box is placed or its workflow changes; they are never written to the YAML.
- **The canvas.** The pane keeps an always-drawn thin scrollbar and fades at its right edge while more of the
  layout is off-screen. A line's label sits above it with a halo when the room between its boxes takes it, and
  otherwise shows only while the line is hovered or selected.
- **Editing.** A single click selects a box and shows a small card; a double-click (or the card's Edit) opens
  the node modal. A bundle opens in place on a double-click, and the card's Edit opens the modal, which has an
  Open button. The node modal commits pending text on Escape and on the backdrop.
- **The wire modal.** An artifact the target reads that any other box writes (or the workflow's inputs) shows
  checked, disabled and says who: the input is the target's, so unchecking it would cut every path in. Removing
  one the target's prompt reads asks first. A loop's `carry` is typed, with the source's outputs offered, since
  it is any key of the result (layer's `carry: feedback`). `connect` merges a second artifact into an existing
  line rather than adding another.
- **The node modal.** A meeting lists its own seats and inputs, the protocol's fixed and any extra (amigos's
  `base`) editable; a button adds a missing protocol name. Picking the check already set changes nothing;
  another keeps the outputs it still offers and every arg, marking one it does not declare. Fields text that
  does not parse keeps the modal open. Renaming an output follows it into the lines' `when`s and the readers'
  `args`.
- **Saving a file.** Save is off while a file's workflow is as the file has it; saving an edit regenerates the
  text, so the file's comments go.
- **The output-format sentence** is inserted by a button, never automatically. A meeting's prompt is required
  (an engine rule).
- **Bundles in place.** Opening a bundle puts `?parent=` beside `?from=`; a breadcrumb leads back and restores
  the parent's unsaved edits from a sessionStorage hold, also on the browser's Back. A hold expires and says
  so when it cannot be kept.
- **The Workflows page** shows "Used by" for each bundle, deletes drafts, and has New.
- **Upgrade.** A draft in the old schema is read upgraded and shows `[data-upgraded]` until saved.
- **Adopt.** The story screen and the card modal offer "Use the current workflow", with a confirm.
- **The run view** drops the `group` bracket (the server never emits it); a bundle's per-item lanes draw as the
  layer lanes.
