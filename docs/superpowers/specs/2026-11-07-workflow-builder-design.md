# The workflow builder: a canvas for composing nodes, drafts that run, publish as a PR

2026-11-07. Brainstormed with Noah in chat. Mockups: https://claude.ai/artifact/RGGKi4dx41sGjv3dHk9dQ3
(three layouts; Noah picked **B, the canvas**). Follows `2026-11-06-generic-nodes-design.md` (Part 1 built,
PR #256); precedes Part 2 of that design.

## Why

Part 1 made the story pipeline a workflow of typed nodes, described in YAML under `config/workflows/`.
The next workflows Noah has in mind are a bug fix (lighter scrutiny than a fresh build) and deep research,
with more after them. Today the only way to make one is to hand-write YAML, and every card runs the story
workflow because the line hard-wires it. Noah wants both ways in: a GUI that builds workflows and
infrastructure-as-code where the YAML in the repo stays the truth.

## Decisions Noah made

- **D1.** YAML in the repo is the source of truth for workflows.
- **D2.** The card gets a **Workflow** field, mandatory. Its list holds every workflow plus **Build new…**,
  which opens the builder. When the builder is done, the user returns to the card with the new workflow
  selected and every field they had edited on the card still as they left it.
- **D3.** The card's type picks the workflow: a workflow declares the type it serves.
- **D4.** The builder is a **canvas**: a palette of node types, free placement, wires you drag (option B).
- **D5.** **Drafts live in the board's database and run right away.** A workflow built in the GUI is a
  draft until published; a card can name a draft and run on it.
- **D6.** **Publish writes the YAML and opens a PR.** Only a human merges. Once merged, the file is the
  truth and the draft is gone.
- **D7.** Finish line: the builder, the card field, drafts that run, publish as PR, and the line running
  whichever workflow a card names. The bug-fix workflow itself is not part of this round. Then back to
  generic nodes Part 2.

## Goals

1. Open the builder from a card ("Build new…") or from a Workflows page; make a workflow from the seven
   node types and the existing bundles by dropping boxes and dragging wires; fill each box's contract in a
   form; see validation problems on the box they belong to.
2. Save a draft to the board's database; pick it on a card; run the card on it through the composer.
3. Publish a draft: the YAML file is written on a branch, committed, pushed, and a PR is opened with the
   bot identity, as a story's PRs are. The card shows the workflow with its PR until merged.
4. Open any existing workflow file (the story, the layer bundle) in the builder and publish it unchanged:
   the PR's only diff is the layout block.
5. The line runs the workflow the card names; a story card without one runs the story.
6. A card on a non-story workflow has a run view: its nodes as a row of boxes lit by the node events.

## Non-goals

- The bug-fix and deep-research workflows. They are the first users of the builder, built after it.
- New node types. The palette is the seven types the design fixed (five built, team and pair later).
- New checks or parsers from the GUI: those are code and stay code. The GUI picks from the registry.
- A general card Type field and type-driven pickup rules (the work-types design). This round adds only
  the Workflow field; the list is grouped by the type each workflow declares.
- Editing roles, models or prompts' rules from the builder. The builder links out to where they live.
- Running workflows from anywhere but a card.

## Anti-goals

- **Two sources of truth.** Nothing that is merged ever comes from the database. A draft is a draft:
  marked as such on the card, never read by the line as if it were the file.
- **A GUI that can save what the composer refuses.** Save and Publish run `check_workflows`; a draft with
  problems can be saved as a draft, but cannot be picked on a card, and cannot be published.
- **Losing the card's edits.** Leaving for the builder and coming back must not drop a half-written card.
- **A busier screen than the pipeline view.** Plain boxes in the pipeline screen's six shapes; detail in
  the form panel and on hover (Noah's standing preference).

## What it looks like

**The card** (CreateModal and CardModal) gains a `Workflow` field next to Repository, required to create
or move a card to Approved. The select lists workflows grouped by the type they declare, drafts in their
own group marked "draft", and `Build new…` last. Choosing `Build new…` opens the builder with the card's
draft fields held; Save or Publish in the builder returns to the card with the new workflow selected.

**The builder** (`/workflows/build/<name>` and `/workflows/build/new`):

- Left: the palette. The seven node types as boxes in their pipeline-screen shapes (model step, code
  step, gate), and below them the bundles: every other workflow, draft or file.
- Middle: the canvas. Dotted background, boxes placed where you drop them, wires from a box's right port
  to another's left port. Wire kinds are the five moves: a plain wire is **sequence**; two or more wires
  out of one box that rejoin are **parallel**; a dashed wire back to an earlier box is a **loop**, with
  its max and carry on the wire's form; a wire labelled with an output value is a **branch**; a **for
  each** is a container box holding a bundle, opened on double-click as its own canvas.
- Right: the form for the selected box or wire. The node contract: type (fixed once placed), role (from
  the roles config; several for a meeting), prompt (a registry id, or a prompt file, see below), parse,
  run, inputs, outputs with formats, steps, tools, done, on_fail, when, args. Inputs offer the outputs
  of upstream boxes and the workflow's loose names.
- Top bar: the workflow's name and the type it serves; `Validate`, `Save draft`, `Publish`; and the
  problem count. A problem highlights its box or wire and names the field.
- A YAML tab shows the file the canvas would save, read-only, for the infra-as-code eye.

**Validation** is `check_workflows` from `airlie.flow.spec`, run in the server on the YAML the canvas
produces, so the GUI and a hand-written file are judged by the same code.

## The YAML a workflow carries

Two additions to the Part 1 schema, both optional and both ignored by the composer:

- `type:` on a workflow: the card type it serves (`story`, `bug`, `research`, …). The card's list groups
  by it. `story.yaml` gains `type: story`.
- `layout:` on a workflow: `{node: {x, y}}` for every box the canvas placed. A hand-written file without
  it opens with an automatic left-to-right layout; a published file keeps its arrangement.

`airlie.yaml`'s `includes:` accepts a glob (`config/workflows/*.yaml`), so a published workflow needs no
edit elsewhere. A file's name is the workflow's name.

## Drafts

A table in the board's SQLite (`BoardStore`): name, type, the YAML text (including layout), created,
updated, `published_pr` (null until published), `merged` (set by bookkeeping when the PR merges, after
which the row is deleted). A draft's name must not collide with a file's.

The line resolves a card's workflow by name: the repo's `cfg.workflows` first, then drafts. A draft is
loaded into a `WorkflowSpec` and validated on every pick; an invalid draft cannot be chosen on a card.

## The card's Workflow field

Stored as a card label `workflow:<name>`, the way a card already holds its repository (`repo:<name>`), and
shown as a field. Mandatory
for a card to leave To do: the board refuses to move a card without one to Approved, and the line skips
such a card with a note. When the named workflow is a draft, the field shows "draft"; when it has an open
PR, the PR number; when merged, the plain name.

## Publish

Publish takes a draft and, through the existing `Publisher` (the bot identity, `gh`): creates a branch
`airlie/workflow/<name>` from trunk in the serve's checkout of airlie, writes
`config/workflows/<name>.yaml`, commits with a message naming the workflow and its type, pushes, and opens
a PR. The draft row records the PR. Bookkeeping notices the merge, deletes the draft row, and the card's
field resolves to the file. A workflow that already exists as a file is published the same way: the diff
is the change.

## Running the workflow a card names

`compose(pipe, st, card, …)` runs the card's workflow instead of a fixed `"story"`. The composer accepts
the workflows mapping it runs from (repo files plus drafts) instead of reading only `cfg.workflows`. A
card naming any workflow other than `story` runs through the composer regardless of `story.composer`;
the flag keeps governing the story only, until Part 2 flips it. The registry pieces a workflow names
(`story.*`) are the ones Part 1 registered; a workflow may use any of them.

## Prompts without code (my call, flagged)

A new workflow needs briefs that do not exist yet (a bug fix's reproduce brief). Today a brief is a Python
builder. So the `prompt` field accepts, besides a registry id, a **prompt file**: a markdown file under
the prompts hub (`src/airlie/prompts/rules/<name>.md`) rendered with the node's inputs as placeholders,
through one generic registry piece. The builder can create such a file from the form; Publish commits it
beside the YAML. Checks and parsers stay code.

## Decisions (mine, for Noah to check)

- **D8.** The Workflow field is a card label `workflow:<name>` shown as a field. (me)
- **D9.** The card's Type field waits for the work-types round; the list groups by the workflow's declared
  type. (me)
- **D10.** A card naming a non-story workflow runs through the composer regardless of the flag. (me)
- **D11.** `type:` and `layout:` are optional workflow keys the composer ignores; `includes:` takes a
  glob. (me)
- **D12.** Publish goes through the existing `Publisher` and the bot identity, branch
  `airlie/workflow/<name>`. (me)
- **D13.** Prompt files as a second kind of `prompt`, authored from the builder. (me, flagged)
- **D14.** The canvas may use a Vue canvas library if the builder prefers; it must keep the pipeline
  screen's box shapes. (me)

## Answers from the builder check (my calls unless marked)

- **D15. A generic artifact store.** `StateStore` today refuses any output that is not a `StoryState`
  field, so a new workflow could only rearrange the story's artifacts. `StoryState` gains one field,
  `artifacts: dict[str, Any]`, and `StateStore` routes every declared output that is not a known field
  (and not in the `feedback.`/`package.` memory namespaces) into it, saved with the state. Resume works
  because the skip rule reads the same store. Formats `ref:*` hold their reference string.
- **D16. Lanes.** A workflow with a `planned` node (a check that emits `story_planned`) splits into the
  plan lane and the build lane at that node, as the story does. A workflow without one runs whole in the
  build lane. `_drive`'s prologue applies to every workflow; the GitHub prepare step runs only when the
  workflow has a `publish` node. The exits (block, wait, cancel) are shared.
- **D17. Migration and scope of the field.** Only story-level cards run workflows this round; initiative,
  epic and subtask keep their pipelines. A story card without a `workflow:` label is treated as
  `workflow:story`: existing cards and the cards the epic pipeline makes all run the
  story unchanged. The field is mandatory in the GUI (CreateModal requires it, preselecting `story`;
  CardModal shows it as required), so every card created or edited by hand carries it. Noah confirmed
  2026-11-07: this is what "mandatory" means: mandatory in the GUI, story by default elsewhere.
- **D18. Round trip on publish.** Publishing a workflow that exists as a file edits that file's text in
  place (a round-trip YAML library such as ruamel, or a text patch for the `layout:` block), so comments,
  key order and inline style survive and Goal 4 holds. A brand-new draft is serialised fresh in the
  style of the existing files. Drafts therefore store the YAML text, not a parsed model.
- **D19. Draft prompt files.** A draft's prompt files live in the drafts table beside its YAML (name →
  markdown). The generic prompt piece reads the hub first, then the draft's prompts. Publish commits them
  under `src/airlie/prompts/rules/` beside the YAML. The serve's checkout is never written by a draft.
- **D20. Publish mechanics.** Publish uses a separate worktree of the airlie repo, as `Publisher` does
  for stories, never the serve's own checkout. The bot identity already opens PRs on the airlie repo
  (AIR-* stories), so it publishes workflows too. After a merge the draft row is kept until a config
  reload (serve restart) sees the file; until then the card resolves to the draft, whose text equals the
  merged file. Bookkeeping deletes the row once the file is loaded.
- **D21. A card snapshots its workflow.** When a card starts, the workflow's YAML text is copied into its
  state (`st.workflow`) and every resume runs the snapshot, not the live draft or file. Drafts and files
  may therefore change at any time without breaking a running card. Restart-from-step re-snapshots.
- **D22. A generic run view (Noah, 2026-11-07).** `/workflows/<key>` keeps the story's purpose-built
  screen for cards on the story workflow. A card on any other workflow gets a generic view: one
  left-to-right row of boxes taken from the card's snapshotted workflow spec, in the pipeline screen's
  six shapes (parallel as a bracket, for-each as a lane per item, a gate dashed), each box lit by its
  `node_started` / `node_finished` / `node_skipped` events, with the now bar naming the running node and
  a hover card listing that node's events. Phone: the same one-list treatment as the story screen.
- Route order: `/workflows/build/...` is declared before `/workflows/:key`.

## Left to the builder

- The canvas library (hand-rolled SVG or a Vue flow library), as long as boxes keep the six shapes.
- The exact wire-form fields per move, and how parallel joins are inferred from the graph.
- How the card's draft fields are held across the trip to the builder (router state, a store).
- The drafts table's exact columns and the bookkeeping hook that notices a merge.
- Order of work. A sensible order: the schema additions and glob include; the composer taking a workflows
  mapping and `compose` reading the card's workflow; the drafts table and the card field; the builder's
  canvas read-only from a file; editing and validation; Save draft; Publish; the prompt-file piece.
