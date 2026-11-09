# Nodes Are Config, Plan 2: The Builder — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The workflow builder draws boxes as nodes and lines as artifacts on a full-width canvas, edits a node in a large modal with the prompt as a text area and no dropdown that names a Python function, picks checks from the catalog, opens a bundle in place, lets a card adopt the current workflow, and opens the shipped v2 story and layer files again.

**Architecture:** The data layer (`graph.ts`, `yaml.ts`, `shapes.ts`, `layout.ts`) moves to schema v2: a `GEdge` is a line carrying named artifacts (or nothing, for ordering) with an optional condition and loop bound; a `Graph` has workflow `inputs` and `outputs`; `fromWorkflow`/`toWorkflow`/`toYaml`/`parseWorkflow` read and write `to` lines, `bundle` nodes and `OutputSpec`s. The canvas gains one named port per output and a bundle box with the bundle's declared ports. The right-hand panel goes; `NodeModal`, `WireModal` and `WorkflowModal` replace `NodeForm`, `WireForm` and `WorkflowForm`. The API client follows the server's v2 catalog (`checks` with declared inputs, outputs, args, transient, every_run; `meetings`), `used_by` and the adopt route. Tests read the live `config/workflows/*.yaml`; the frozen v1 copies go.

**Tech Stack:** Vue 3 + TypeScript, Vitest with `@vue/test-utils`, Tailwind tokens, the `yaml` package, Playwright e2e in the Apple container (`task test:e2e`).

**Spec:** `docs/superpowers/specs/2026-11-08-nodes-are-config-design.md` ("The canvas", "Editing", "Bundles and isolation", D4–D8, D23–D24, both as-built sections). Engine contract: the server at main `b6df184` (plan 1 merged): `GET /v1/workflows/catalog` → `{roles, types, formats, kinds, checks: [{name, title, inputs, outputs, formats, transient, every_run, args}], meetings: [{roles, inputs}], prompt_files}`; rows carry `used_by`; `DELETE /v1/workflows/drafts/{name}` and a rename answer 409 `{"used_by": [...]}`; `POST /v1/airlie/stories/{key}/adopt-workflow` → `{key, name, from, to}` (409 running, 422 not adoptable).

## Global Constraints

- The YAML in the repository is the source of truth; `toYaml` writes what the server's schema v2 reads; a drag changes only `layout`; node keys keep file order (a new box goes last).
- No form field offers a Python id on a model node. A check or gate node picks a catalog entry shown by its title with its declared inputs, outputs and args; a meeting shows its fixed roles and inputs from `catalog.meetings`.
- Every page works at phone width (one column, modal as a bottom sheet, 16px gutters, no horizontal page scroll) and in both themes through the existing tokens.
- Tests keep the `data-*` hooks the e2e suite reads; new hooks are `data-*` too. `task web:gate` (lint with 0 errors, 100% of tests) passes after every task; `task test:e2e -- -k "workflow_builder or board_modal"` passes at the end.
- Commit after every task. Subjects start with `builder:`.

## Review Focus

1. **A line is dropped onto a box that already reads that artifact.** No duplicate input, no duplicate line; the existing line is selected. Pinned in Task 3.
2. **An output is renamed while lines carry it.** The lines and the consumers' inputs follow the new name, or the rename is refused with the consumers listed. Pinned in Task 4.
3. **A condition with a typo.** Validate shows the server's message on the wire and in the problems list; the draft still saves (drafts may carry problems). Pinned in Task 5.
4. **Opening a bundle in place with unsaved edits on the parent.** Returning by the breadcrumb restores the parent's edits. Pinned in Task 6.
5. **Adopting the current workflow on a running story.** The button is disabled while the story runs, and a 409 from the server shows as the reason. Pinned in Task 7.

---

## File Structure

**Create:**
- `web/src/screens/builder/NodeModal.vue`, `WireModal.vue`, `WorkflowModal.vue`, `BoxCard.vue` (the small selection card), `OutputsEditor.vue`, `CatalogPicker.vue`, `fields.ts` (field-kind helpers and the format sentence), `builder_state.ts` (the held parent graph for open-in-place).
- `web/src/api/stories.ts` (`adoptWorkflow`) or the function in `web/src/api/workflows.ts`.
- Tests beside each.

**Modify:**
- `web/src/screens/builder/graph.ts`, `yaml.ts`, `shapes.ts`, `layout.ts`, `Canvas.vue`, `BuilderBox.vue`, `Palette.vue`, `Problems.vue`, `useBuilder.ts`, `web/src/screens/BuilderScreen.vue`.
- `web/src/api/workflow_defs.ts`, `web/src/screens/workflows/DefinitionsTable.vue`, `web/src/screens/WorkflowsScreen.vue`, `web/src/screens/WorkflowScreen.vue`, `web/src/screens/workflow/Pipeline.vue`, `web/src/api/workflows.ts`.
- `web/src/test-utils/workflow-yaml.ts` (reads the live files), `tests/e2e/test_workflow_builder.py`, `AGENTS.md`, the design doc.

**Delete:** `NodeForm.vue`, `WireForm.vue`, `WorkflowForm.vue` and their tests; `web/src/test-utils/workflows-v1/`.

---

### Task 1: The data layer on schema v2

**Files:**
- Modify: `web/src/screens/builder/graph.ts`, `yaml.ts`, `shapes.ts`, `layout.ts`, `web/src/test-utils/workflow-yaml.ts`
- Delete: `web/src/test-utils/workflows-v1/`
- Test: `graph.test.ts`, `graph.edit.test.ts`, `yaml.test.ts`, `layout.test.ts`

**Interfaces:**
- `NODE_TYPES = ["ask", "conversation", "write", "meeting", "team", "check", "gate", "bundle"] as const`; `FORMATS` unchanged; `KINDS` (text, number, bool, list of …, one of …).
- `type OutputSpec = { format: string; fields?: Record<string, string>; transient?: boolean }`; `type WorkflowSpec = { type?: string | null; inputs: string[]; outputs: string[]; nodes: Record<string, NodeSpec> }`.
- `type GEdge = { from: string; to: string; artifacts: string[]; when?: string; loop?: { max: number | string; carry?: string } }` (empty `artifacts` = an ordering line); `type Graph = { name; type; inputs; outputs; nodes: GNode[]; edges: GEdge[]; bundles: Record<string, Graph> }`.
- `fromWorkflow(name, spec, layout={}) -> Graph`: one edge per `to` line; `artifacts` = the `from` node's output names (item-substituted as written) that the `to` node lists in `inputs`.
- `toWorkflow(graph) -> {spec, layout}`: lines from edges, `when`/`loop` kept; a bare string line when neither is set.
- `connect(graph, from, to, opts: { artifacts?: string[]; when?: string; loop?: GEdge["loop"] }) -> Graph`: adds the line and adds each artifact to the target's `inputs` if absent; a second line between the same nodes replaces the first.
- `removeEdge(graph, edge) -> Graph` removes the line and the artifacts it alone carried from the target's inputs.
- `addNode(graph, type, x, y, spec={})` defaults: ask/conversation/write `{role: "", prompt: "", inputs: [], outputs: {}}`; meeting `{role: [...catalog meeting roles], prompt: "", inputs: ["story","layers"], outputs: {}}` (roles handed in by the caller); team `{role: {supervisor: "", worker: ""}, prompt: "", worker: "", inputs: [], outputs: {}}`; check/gate `{run: "", inputs: [], outputs: {}}`; bundle `{workflow: "", inputs: [], outputs: {}}`.
- `renameNode` retargets `to` lines and `feedback.<id>`; `renameOutput(graph, id, old, new) -> Graph | { refused: string[] }` renames the output, the lines' `artifacts` and every consumer's inputs (and `{old}` placeholders in their prompt text), refusing when a consumer's prompt file (not inline text) names it.
- `readable(graph, id) -> string[]`: workflow inputs + other boxes' output names + `feedback.<id>`.
- `parseWorkflow(text) -> {name, spec, layout}` and `toYaml(graph)` read and write v2; the shipped story and layer files round-trip (`parseWorkflow(toYaml(fromWorkflow(parse(file))))` equals the parsed file's nodes).
- `shapes.ts`: `Shape = "model" | "code" | "gate" | "bundle"`; team is model; bundle is bundle.
- `autoLayout`: columns from the longest run of lines.
- `web/src/test-utils/workflow-yaml.ts` exports `STORY_YAML`/`LAYER_YAML` read from `../../../config/workflows/*.yaml?raw` (vite `server.fs.allow` already covers it).

- [ ] **Step 1: Tests** — `fromWorkflow(story)` yields the file's nodes and a line per `to` entry with the right artifacts (e.g. `survey → survey_note` carries `survey`; `gate → survey` and `gate → refine` are two lines); round trip of both shipped files; `connect` adds the input once; `removeEdge` drops only what the line alone carried; `renameOutput` follows lines and inputs and refuses a prompt-file consumer; `addNode` defaults per type; `shapeOf("bundle") === "bundle"`.
- [ ] **Step 2–5:** fail, implement, `task web:gate`, commit — `builder: the graph, yaml and shapes read and write schema v2; tests read the shipped files`

### Task 2: The catalog and API client on v2

**Files:**
- Modify: `web/src/api/workflow_defs.ts`, `web/src/api/workflows.ts`, `web/src/screens/builder/useBuilder.ts`
- Test: `web/src/api/workflow_defs.test.ts`, `useBuilder.test.ts`

**Interfaces:**
- `type CheckDef = { name; title; inputs: string[]; outputs: string[]; formats: Record<string,string>; transient: string[]; every_run: boolean; args: string[] }`; `type MeetingDef = { roles: string[]; inputs: string[] }`; `type Catalog = { roles: string[]; types: string[]; formats: string[]; kinds: string[]; checks: CheckDef[]; meetings: MeetingDef[]; prompt_files: string[] }`; `EMPTY_CATALOG` accordingly.
- `WorkflowDef` gains `used_by: string[]`.
- `adoptWorkflow(key: string): Promise<{ key; name; from: string | null; to: string | null }>` (POST `/v1/airlie/stories/{key}/adopt-workflow`).
- `usedBy(e: ApiError): string[] | null` parses a 409's `{used_by}` detail; `useBuilder.remove()` returns `{refused: string[]}` on it.
- `useBuilder.place(item)` places a `bundle` node for a bundle item (no `each` box).

- [ ] **Step 1: Tests** — the catalog type matches a recorded server payload (fixture copied from a `GET /v1/workflows/catalog` of the shipped server: keep it in `web/src/test-utils/catalog.json`); `adoptWorkflow` posts to the right path; a 409 delete yields `{refused: [...]}`.
- [ ] **Step 2–5:** fail, implement, gate, commit — `builder: the client reads the v2 catalog, used_by and the adopt route`

### Task 3: The canvas: ports, lines, bundle boxes, full width

**Files:**
- Modify: `Canvas.vue`, `BuilderBox.vue`, `Palette.vue`, `BuilderScreen.vue` (the row loses the Fields aside; the canvas takes the width)
- Create: `BoxCard.vue`
- Test: `Canvas.test.ts`, `BuilderBox.test.ts`, `Palette.test.ts`, `BuilderScreen.test.ts`

**Interfaces:**
- A box shows one out-port per output: `span[data-port=out][data-node][data-artifact=<name>]`, stacked on the right edge, plus one edge port `span[data-port=edge][data-node]` for an ordering line; one in-port on the left. A bundle box's ports are the bundle workflow's declared `outputs` (out) and its `inputs` (in-port label on hover).
- Dragging from an out-port and releasing on a box emits `connect(from, to, {artifacts: [name]})`; from the edge port, `connect(from, to, {})`. Releasing on a box that already reads the artifact selects the existing line and emits nothing else (Review Focus 1).
- Every line shows `text[data-edge-label]` with its artifacts joined by ", " (or "order" for an ordering line), a condition prefix `if <when>` when set, and a loop drawn dashed below the boxes with `loop ×<max>` in the label.
- Click on a box emits `select`; the canvas shows `BoxCard` (`[data-box-card]`: name, type, an `Edit` button `[data-box-edit]`) near the box; double-click or Edit emits `open(id)`. Click on a line emits `selectEdge`; double-click emits `openEdge(edge)`.
- `Palette` lists the seven node types and bundles (`[data-bundle]`), dropping as `bundle` boxes.
- `BuilderScreen` renders `grid-cols-[180px_minmax(0,1fr)]` on desktop, the canvas full height (as today), no aside.

- [ ] **Step 1: Tests** — ports per output on the story's `gate` box (one `gate` out-port) and on `plan_layers` ...; a drag from the `survey` port to `survey_note` emits `connect` with `["survey"]`; a drop on a box already reading it emits `selectEdge` only; labels render artifacts, `if …`, `loop ×2`; the bundle box shows `built.{item}`/`satisfied.{item}` out-ports; double-click emits `open`; the palette has no parallel/each and lists bundles.
- [ ] **Step 2–5:** fail, implement, gate, commit — `builder: ports per output, lines labelled with what they carry, bundle boxes with ports, the canvas full width`

### Task 4: The node modal

**Files:**
- Create: `NodeModal.vue`, `OutputsEditor.vue`, `CatalogPicker.vue`, `fields.ts`
- Delete: `NodeForm.vue`, `NodeForm.test.ts`
- Modify: `BuilderScreen.vue`, `useBuilder.ts`
- Test: `NodeModal.test.ts`, `OutputsEditor.test.ts`, `CatalogPicker.test.ts`, `fields.test.ts`

**Interfaces:**
- `NodeModal` props `{ open: boolean; node: GNode; graph: Graph; catalog: Catalog; prompts: Record<string,string>; bundles: string[] }`, emits `update:open`, `update [spec]`, `rename [id]`, `renameOutput [old, new]`, `remove`, `createPrompt [id, text]`. The Dialog uses the app's existing dialog component sized to most of the window (`max-w-[min(96vw,1100px)]`, height `min(92dvh, …)`), a bottom sheet at phone width.
- Fields by type (`[data-form-field=<name>]`, `[data-input=<name>]`):
  - ask/conversation/write: `role` select from `catalog.roles`; `prompt` **textarea** `[data-input=prompt]` taking the bulk of the modal, with the node's inputs listed beside it as chips `[data-prompt-insert=<name>]` that insert `{name}` at the caret, and a "Use a prompt file…" control that switches the field to a `prompt:<id>` select (hub ids + draft prompts, with the existing PromptFileDialog to create one); `turn` textarea for conversation; `inputs` checklist from `readable(graph, id)` (`[data-input-name]`); `outputs` via `OutputsEditor`; `steps`, `tools`, `on_fail`, `events` (done/failed kinds), `session` (select among upstream `session` outputs whose producer has the same role; disabled with the reason otherwise), `advisor`.
  - team: `role.supervisor`/`role.worker` selects, `prompt` textarea, `worker` textarea (its chips include `{assignment}`), `workers` number, the rest as above.
  - meeting: roles and inputs shown read-only from `catalog.meetings[0]` (`[data-meeting-fixed]`), `prompt`/`turn` textareas optional, outputs.
  - check/gate: `CatalogPicker` (`[data-catalog-pick]`): a searchable list of checks by `title`, each showing name, inputs, outputs, args; picking sets `run`, fills `outputs` from `formats`/`transient`, lists required `args` as fields (`[data-arg=<name>]`), and marks declared inputs missing from the node's inputs (`[data-input-missing]`) with one click to add them.
  - bundle: `workflow` select (bundles), `each` select among the node's list inputs, the bundle's declared inputs/outputs shown (`[data-bundle-ports]`).
- `OutputsEditor` props `{ outputs: Record<string, OutputSpec>; kinds: string[]; formats: string[] }`, emits `update`: rows `[data-output-row]` with name, format select, a `fields` textarea (one `name: kind` per line, validated against `fields.parseKind`), a `transient` checkbox; `[data-output-add]`, `[data-output-remove]`.
- `fields.ts`: `parseKind(text) -> Kind | Error` mirroring the server grammar; `formatSentence(fields) -> string` producing the `output-format` rule's sentence (fixture copied from `src/airlie/prompts/rules/output-format.md`); `NodeModal` offers `[data-insert-format]` to append it to the prompt when a fielded output exists (Ruling 5 of plan 1: never automatic).
- Rename of an output goes through `renameOutput`; a refusal shows the consumers (`[data-rename-refused]`) (Review Focus 2).

- [ ] **Step 1: Tests** — the prompt is a textarea, never a select; inserting a chip writes `{survey}`; a check picked from the catalog fills outputs and shows missing inputs; the team form shows two role selects and `{assignment}`; the meeting form shows fixed roles; `formatSentence` equals the rule's text for a two-field example; an output rename refused lists consumers; the modal mounts as a bottom sheet under 640px (class assertion).
- [ ] **Step 2–5:** fail, implement, gate, commit — `builder: a node is edited in a large modal; the prompt is a text area; checks come from the catalog`

### Task 5: The wire modal and the workflow modal

**Files:**
- Create: `WireModal.vue`, `WorkflowModal.vue`
- Delete: `WireForm.vue`, `WorkflowForm.vue` and tests
- Modify: `BuilderScreen.vue`, `Problems.vue`
- Test: `WireModal.test.ts`, `WorkflowModal.test.ts`, `BuilderScreen.test.ts`

**Interfaces:**
- `WireModal` props `{ open; edge: GEdge | null; from: GNode; to: GNode; graph: Graph }`, emits `update:open`, `apply [{artifacts, when?, loop?}]`, `remove`: artifacts as checkboxes over `from`'s outputs (`[data-wire-artifact=<name>]`), "order only" when none; `when` as a text input `[data-wire-when]` with the grammar hint; `loop` toggle with `max` (number or `config.…` path) and `carry`; `[data-wire-apply]`, `[data-wire-remove]`.
- `WorkflowModal` props `{ open; type; inputs; outputs; types: string[] }`, emits `update [{type?, inputs?, outputs?}]`: type select (blank = bundle), inputs and outputs as chip editors (`[data-workflow-inputs]`, `[data-workflow-outputs]`). Opened from a title-bar button `[data-workflow-edit]`.
- Validation stays server-side: after `apply`, `useBuilder.validate()` runs when the graph was validated before; a condition problem appears in `Problems` and the wire's label gets `[data-edge-problem]` (Review Focus 3: the draft still saves).

- [ ] **Step 1: Tests** — a wire's artifacts and condition save into the line; a loop with `max: 2` draws as a loop; a condition problem from a mocked `/validate` marks the wire and the list; the workflow modal edits inputs/outputs; the title bar opens it.
- [ ] **Step 2–5:** fail, implement, gate, commit — `builder: lines and the workflow are edited in modals; conditions and loops live on the line`

### Task 6: Bundles in place, use tracking, the Workflows page

**Files:**
- Create: `builder_state.ts`
- Modify: `BuilderScreen.vue`, `useBuilder.ts`, `DefinitionsTable.vue`, `WorkflowsScreen.vue`, `web/src/router` (the builder route accepts `?from=<name>`)
- Test: `BuilderScreen.test.ts`, `DefinitionsTable.test.ts`, `builder_state.test.ts`

**Interfaces:**
- `builder_state.ts`: `holdGraph(name, graph, prompts) -> token`, `takeGraph(token) -> {name, graph, prompts} | null` in `sessionStorage` (`airlie.builderHold`), like `card_draft.ts`.
- Double-click on a bundle box: `holdGraph` the parent, push `{name: "builder", params: {name: <bundle>}, query: {from: token}}`; the builder shows a breadcrumb `[data-breadcrumb]` "‹ <parent name>" that pushes back to the parent and `takeGraph` restores its unsaved edits (Review Focus 4).
- `DefinitionsTable` gains a "Used by" column (`[data-def-used-by]`, the names joined) and a Delete control for drafts (`[data-def-delete]`, confirm dialog); a 409 shows "used by: …" inline (`[data-def-refused]`). `WorkflowsScreen` gains a "New workflow" button `[data-new-workflow]` to `/workflows/build/new`.
- `BuilderScreen`'s own delete shows the 409's names.

- [ ] **Step 1: Tests** — double-click a bundle box navigates with a token and the breadcrumb restores the held graph; the table shows used_by and refuses a delete with the list; the New button navigates.
- [ ] **Step 2–5:** fail, implement, gate, commit — `builder: open a bundle in place with a breadcrumb back; the Workflows page shows who uses a bundle and deletes drafts`

### Task 7: Adopt the current workflow from the story screen

**Files:**
- Modify: `web/src/screens/WorkflowScreen.vue`, `web/src/api/workflows.ts` (or the Task 2 function), `web/src/screens/board/CardModal.vue`
- Test: `WorkflowScreen.test.ts`, `CardModal.test.ts`

**Interfaces:**
- In the story screen's header, a button `[data-adopt]` "Use the current workflow", disabled with `title` "the story is running" while `live`/running (Review Focus 5); click opens a confirm dialog `[data-adopt-dialog]` saying changed nodes and everything after them run again; on success `[data-adopted]` shows "now on <name> (<from> → <to>)" and the flow refreshes; a 409/422 shows the server's message `[data-adopt-error]`.
- `CardModal`: for a started card the locked Workflow field gains a link "use the current workflow" that opens the same dialog.

- [ ] **Step 1: Tests** — the button is disabled while running; a mocked 200 shows the result and refreshes; a 409 shows the message; the card modal link appears only for started cards.
- [ ] **Step 2–5:** fail, implement, gate, commit — `builder: a story adopts the current workflow from its screen and its card`

### Task 8: Run view readers, e2e, docs

**Files:**
- Modify: `web/src/screens/workflow/Pipeline.vue`, `web/src/api/workflows.ts` (drop `group`), `tests/e2e/test_workflow_builder.py`, `AGENTS.md`, `README.md`, the design doc (as-built, plan 2)
- Test: `Pipeline.test.ts`, the e2e suite

**Interfaces:**
- `Pipeline.vue` drops the `group` bracket (the server never emits it); a bundle's per-item lanes render as today's layer lanes.
- e2e test 1: the lone check box is configured through the catalog picker (`[data-catalog-pick]` → the `intake` entry), then validate, save as `mini`/`bug`, return to the card; test 2: `/workflows/build/story` shows one box per shipped node (live count), `intake`, `amigos`, `review` present, the type chip `story`; a new test: double-click the `build_layers` bundle opens `layer` with a breadcrumb back.
- As-built (plan 2): what differs from the design text (ports per output; the small card + double-click; the wire modal fields; the breadcrumb hold; the format sentence inserted by a button; the Workflows page New and Delete).

- [ ] **Step 1: Tests** — `Pipeline` with and without lanes; e2e in the container: `task test:e2e -- -k "workflow_builder or board_modal"`, read the evidence screenshot of the story canvas and confirm boxes, ports and labels are visible.
- [ ] **Step 2–5:** fail, implement, `task --force web:gate` + e2e, commit — `builder: the run view reads v2; e2e drives the catalog picker and the bundle breadcrumb; as-built notes`

---

## Self-review

- **Spec coverage:** The canvas (ports, lines, parallel by drawing, join by inputs, conditions and loops on lines, per-item bundles, layout only) → T1, T3, T5; Editing (text area, modal, click/double-click, full width, phone sheet) → T3, T4; Bundles and isolation (ports on a bundle box, open in place, use tracking, adopt) → T1, T3, T6, T7; the catalog view and no code ids → T2, T4; the builder reads the v2 files again → T1, T8; D21 "write this node's prompt" problems show → existing problems list (server-provided), T6 shows them on rows.
- **Type consistency:** `GEdge.artifacts`, `OutputSpec`, `CheckDef`, `MeetingDef`, `renameOutput`, `holdGraph/takeGraph`, `adoptWorkflow` defined where first named and reused by those names.
- **Review Focus:** 1 → T3; 2 → T4 (via T1's `renameOutput`); 3 → T5; 4 → T6; 5 → T7.
- **Proportion:** eight tasks, interfaces and hooks named, no component bodies.
