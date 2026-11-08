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
| 12 checks by `run:` id | the same 12 from the catalog, with inputs and outputs declared |
| the layer's `package` ask and `build` write by `run:` id | package: ask with the package brief as text and `package: json` fields; build: Part 2's split |

The briefs' text moves out of Python into the prompt files the builder already reads (`prompt:<id>`, hub
wins). Where a brief today assembles files and sections by code (the layer-build recipe), the recipe becomes
an input-rendering rule of the prompt file, not a Python function. Nothing a brief says to a model changes
in this round; the trace diff (Part 2, Task 6) proves it.

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

## Left to the builder

- The exact grammar of `{name}` and `{name.field}` in prompt text, and how a list input renders.
- How the catalog reads a check function's signature (type hints, or a small declaration).
- The modal's layout; the phone sheet.
- Whether the YAML keeps `next` as a shorthand for a single unconditional line.

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
