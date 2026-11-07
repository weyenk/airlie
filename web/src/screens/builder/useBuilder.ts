// The builder's state: the definition loaded, the graph being edited, the draft's prompt files, what validation
// said, and saving. BuilderScreen draws it; every edit goes through the pure functions of graph.ts.
import { computed, ref } from "vue";
import type { Ref } from "vue";
import { ApiError } from "@/api";
import { getWorkflowDef, publishWorkflow, registryCatalog, saveDraft, validateWorkflow } from "@/api/workflow_defs";
import type { Catalog, WorkflowDetail } from "@/api/workflow_defs";
import { addNode, emptyGraph, freeId, fromWorkflow } from "./graph";
import type { GEdge, Graph, NodeType } from "./graph";
import { autoLayout, COLUMN } from "./layout";
import { problemsByNode } from "./problems";
import { parseWorkflow, toYaml } from "./yaml";

/** The name the server checks a canvas not yet named under (`new` is the builder's address, not a name). */
const UNNAMED = "untitled";

const EMPTY_CATALOG: Catalog = { roles: [], types: [], prompts: [], parsers: [], checks: [], predicates: [], afters: [], turns: [], meetings: [], prompt_files: [] };

export function useBuilder(name: Ref<string>) {
  const detail = ref<WorkflowDetail | null>(null);
  const graph = ref<Graph>(emptyGraph("new"));
  const prompts = ref<Record<string, string>>({});
  const error = ref<Error | string | null>(null);
  /** The YAML text could not be drawn: the canvas is empty and saving is off. */
  const broken = ref(false);
  const selected = ref<string | null>(null);
  const selectedEdge = ref<GEdge | null>(null);
  /** What the last check (the loaded definition's, a Validate, a save) said, and of which state. */
  const problems = ref<string[]>([]);
  const checked = ref<string | null>(null);
  /** A Validate or a save has answered since the definition loaded (so "no problems" is news worth showing). */
  const validated = ref(false);
  const saving = ref(false);
  const publishing = ref(false);
  /** What the last publish said when it opened nothing (409: the trunk already holds the definition). */
  const publishStatus = ref<string | null>(null);
  const catalog = ref<Catalog>(EMPTY_CATALOG);
  /** The catalog did not load: the forms offer only what the boxes already have (the screen says so, with Retry). */
  const catalogFailed = ref(false);
  async function loadCatalog(): Promise<void> {
    try {
      catalog.value = await registryCatalog();
      catalogFailed.value = false;
    } catch {
      catalogFailed.value = true;
    }
  }
  void loadCatalog();

  const isNew = computed(() => name.value === "new");
  /** The state Save would send; `saved` is the one last loaded or saved. */
  const state = () => `${toYaml(graph.value)}\n${JSON.stringify(prompts.value)}`;
  const saved = ref("");
  const dirty = computed(() => !broken.value && state() !== saved.value);
  const byNode = computed(() => problemsByNode(problems.value));
  /** What Save would send (the text itself only when it could not be drawn). */
  const yamlText = computed(() => (broken.value && detail.value ? detail.value.yaml : toYaml(graph.value)));
  /** Publish opens a PR of a saved draft: not a new canvas, a file's workflow, or changes not yet saved. */
  const unsaved = computed(() => broken.value || isNew.value || dirty.value || detail.value?.source !== "draft");

  let loading = 0;
  async function load(wanted: string): Promise<void> {
    const mine = ++loading;
    detail.value = null;
    error.value = null;
    broken.value = false;
    selected.value = null;
    selectedEdge.value = null;
    problems.value = [];
    prompts.value = {};
    graph.value = emptyGraph(wanted);
    saved.value = state();
    checked.value = null;
    validated.value = false;
    publishStatus.value = null;
    if (wanted === "new") return;
    let def: WorkflowDetail;
    try {
      def = await getWorkflowDef(wanted);
    } catch (e) {
      if (mine === loading) error.value = e instanceof Error ? e : String(e);
      return;
    }
    if (mine !== loading) return; // the address moved on while this one loaded
    detail.value = def;
    problems.value = def.problems;
    prompts.value = { ...def.prompts };
    try {
      const parsed = parseWorkflow(def.yaml);
      // a box keeps the place the definition saved for it; the rest are laid out left to right
      graph.value = autoLayout(fromWorkflow(parsed.name, parsed.spec, { ...parsed.layout, ...def.layout }));
    } catch (e) {
      graph.value = emptyGraph(def.name);
      broken.value = true;
      error.value = `The YAML could not be drawn: ${e instanceof Error ? e.message : String(e)}`;
    }
    saved.value = state();
    checked.value = saved.value;
  }

  /** Apply an edit; one graph.ts refuses (a name taken, a move missing a field) shows as the error. */
  function edit(change: (g: Graph) => Graph): boolean {
    try {
      graph.value = change(graph.value);
      error.value = null;
      return true;
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
      return false;
    }
  }

  /** A box of `type` (or an each box running `bundle`) at (x, y), selected; without a place, right of the rest. */
  function place(item: { type: NodeType } | { bundle: string }, x?: number, y?: number) {
    const type: NodeType = "type" in item ? item.type : "each";
    const id = freeId(graph.value, type);
    const at = x !== undefined && y !== undefined ? { x, y } : { x: graph.value.nodes.length ? Math.max(...graph.value.nodes.map((n) => n.x)) + COLUMN : 0, y: 0 };
    if (edit((g) => addNode(g, type, at.x, at.y, "bundle" in item ? { workflow: item.bundle } : {}))) {
      selected.value = id;
      selectedEdge.value = null;
    }
  }

  async function validate(): Promise<void> {
    const named = isNew.value ? { ...graph.value, name: UNNAMED } : graph.value;
    const now = state();
    try {
      problems.value = await validateWorkflow(toYaml(named), prompts.value);
      checked.value = now;
      validated.value = true;
      error.value = null;
    } catch (e) {
      error.value = e instanceof Error ? e : String(e);
    }
  }

  /** Save the canvas as draft `as` serving `type`. Resolves to true once saved; to `{ refused }` with the
   * server's reason when it refuses the name or the text (409: a file of the repo's has it; 422: reserved, or the
   * YAML does not read), for the name dialog to ask again; false on any other failure (shown as the error). An
   * unchanged draft sends its own text, so a hand-written file keeps its comments. */
  async function save(as: string, type: string | null): Promise<boolean | { refused: string }> {
    const g: Graph = { ...graph.value, name: as, type };
    const same = !dirty.value && detail.value?.source === "draft" && detail.value.name === as && detail.value.type === type;
    saving.value = true;
    try {
      const done = await saveDraft(as, { type, yaml: same ? detail.value!.yaml : toYaml(g), prompts: prompts.value });
      graph.value = g;
      detail.value = done;
      problems.value = done.problems;
      saved.value = state();
      checked.value = saved.value;
      validated.value = true;
      error.value = null;
      return true;
    } catch (e) {
      if (e instanceof ApiError && (e.status === 409 || e.status === 422)) return { refused: e.message };
      error.value = e instanceof Error ? e : String(e);
      return false;
    } finally {
      saving.value = false;
    }
  }

  /** Open the saved draft's PR. Resolves to true once opened (the definition's `pr` set to it); false when the
   * server refused: its problems (422) shown on the canvas, nothing to publish (409) said as `publishStatus`, any
   * other failure (502 the publisher, 404 the draft gone) shown as the error. */
  async function publish(): Promise<boolean> {
    const as = detail.value?.name ?? name.value;
    publishing.value = true;
    publishStatus.value = null;
    try {
      const pr = await publishWorkflow(as);
      if (detail.value) detail.value = { ...detail.value, pr };
      error.value = null;
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        problems.value = refusedProblems(e.message);
        checked.value = state();
        validated.value = true;
        error.value = null;
      } else if (e instanceof ApiError && e.status === 409) {
        publishStatus.value = "Nothing to publish: the trunk already holds this definition";
        error.value = null;
      } else error.value = e instanceof Error ? e : String(e);
      return false;
    } finally {
      publishing.value = false;
    }
  }

  return {
    detail,
    graph,
    prompts,
    error,
    broken,
    selected,
    selectedEdge,
    problems,
    validated,
    stale: computed(() => checked.value !== null && checked.value !== state()),
    byNode,
    catalog,
    catalogFailed,
    loadCatalog,
    saving,
    publishing,
    publishStatus,
    unsaved,
    isNew,
    dirty,
    yamlText,
    load,
    edit,
    place,
    validate,
    save,
    publish,
  };
}

/** The problems of a 422 publish: its detail is `{problems: [...]}`, which arrives as JSON text; anything else is
 * the one problem. */
function refusedProblems(message: string): string[] {
  try {
    const found = (JSON.parse(message) as { problems?: unknown } | null)?.problems;
    if (Array.isArray(found) && found.every((p) => typeof p === "string")) return found;
  } catch {
    // not JSON: the message is the problem
  }
  return [message];
}
