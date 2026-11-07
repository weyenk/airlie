// The builder's state: the definition loaded, the graph being edited, the draft's prompt files, what validation
// said, and saving. BuilderScreen draws it; every edit goes through the pure functions of graph.ts.
import { computed, ref } from "vue";
import type { Ref } from "vue";
import { ApiError } from "@/api";
import { getWorkflowDef, registryCatalog, saveDraft, validateWorkflow } from "@/api/workflow_defs";
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
  const catalog = ref<Catalog>(EMPTY_CATALOG);
  void registryCatalog()
    .then((c) => (catalog.value = c))
    .catch(() => undefined); // the forms offer what the box already has; validation still judges

  const isNew = computed(() => name.value === "new");
  /** The state Save would send; `saved` is the one last loaded or saved. */
  const state = () => `${toYaml(graph.value)}\n${JSON.stringify(prompts.value)}`;
  const saved = ref("");
  const dirty = computed(() => !broken.value && state() !== saved.value);
  const byNode = computed(() => problemsByNode(problems.value));
  const yamlText = computed(() => (broken.value || (!dirty.value && detail.value) ? detail.value!.yaml : toYaml(graph.value)));

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

  /** Save the canvas as draft `as` serving `type`. Resolves to "taken" when the server refuses the name (a file
   * of the repo's), true once saved, false on any other failure (shown as the error). An unchanged draft sends
   * its own text, so a hand-written file keeps its comments. */
  async function save(as: string, type: string | null): Promise<boolean | "taken"> {
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
      if (e instanceof ApiError && e.status === 409) return "taken";
      error.value = e instanceof Error ? e : String(e);
      return false;
    } finally {
      saving.value = false;
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
    saving,
    isNew,
    dirty,
    yamlText,
    load,
    edit,
    place,
    validate,
    save,
  };
}
