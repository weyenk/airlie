// The builder's state: the definition loaded, the graph being edited, the draft's prompt files, what validation
// said, and saving. BuilderScreen draws it; every edit goes through the pure functions of graph.ts.
import { computed, ref } from "vue";
import type { Ref } from "vue";
import { ApiError } from "@/api";
import { deleteDraft, EMPTY_CATALOG, getWorkflowDef, publishWorkflow, registryCatalog, saveDraft, usedBy, validateWorkflow } from "@/api/workflow_defs";
import type { Catalog, WorkflowDetail } from "@/api/workflow_defs";
import { addNode, emptyGraph, freeId, fromWorkflow, renameOutput } from "./graph";
import type { GEdge, Graph, NodeSpec, NodeType } from "./graph";
import { autoLayout, COLUMN } from "./layout";
import { problemsByEdge, problemsByNode } from "./problems";
import { promptFieldsOf } from "./promptFile";
import { parseWorkflow, toYaml } from "./yaml";

/** The name the server checks a canvas not yet named under (`new` is the builder's address, not a name). */
const UNNAMED = "untitled";

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
  // only this workflow's own problems mark its boxes and lines: a bundle's child is checked with it, and a child's
  // node or line of the same name is not this one's
  const own = computed(() => (isNew.value ? UNNAMED : graph.value.name));
  const byNode = computed(() => problemsByNode(problems.value, own.value));
  const byEdge = computed(() => problemsByEdge(problems.value, own.value));
  /** The draft came back upgraded from an older schema: its text is not the stored text until a save. */
  const upgraded = computed(() => detail.value?.upgraded === true); // a save answers with the stored text: false
  /** What Save would send (the text itself only when it could not be drawn). */
  const yamlText = computed(() => (broken.value && detail.value ? detail.value.yaml : toYaml(graph.value)));
  /** Publish opens a PR of a saved draft (an edit of a file too): not a new canvas, a file's workflow as it is, or
   * changes not yet saved. */
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
    publishing.value = false; // a publish still out belongs to the workflow left
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

  /** Put back a canvas held while its bundle was open (builder_state.ts): its graph and prompt files over the
   * definition `load` just read, so the differences show as unsaved changes. Nothing when it is another
   * workflow's. */
  function restore(held: { name: string; graph: Graph; prompts: Record<string, string> }): void {
    if (held.name !== name.value || broken.value) return;
    graph.value = held.graph;
    prompts.value = { ...held.prompts };
    selected.value = null;
    selectedEdge.value = null;
  }

  /** Apply an edit; one graph.ts refuses (a name taken, a move missing a field) shows as the error. */
  function edit(change: (g: Graph) => Graph): boolean {
    try {
      graph.value = change(graph.value);
      error.value = null;
      publishStatus.value = null; // "nothing to publish" was said of the canvas before this edit
      return true;
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
      return false;
    }
  }

  /** A box of `type` (or a bundle box running `bundle`) at (x, y), selected; without a place, right of the rest. */
  function place(item: { type: NodeType } | { bundle: string }, x?: number, y?: number) {
    const type: NodeType = "type" in item ? item.type : "bundle";
    const id = freeId(graph.value, type);
    const at = x !== undefined && y !== undefined ? { x, y } : { x: graph.value.nodes.length ? Math.max(...graph.value.nodes.map((n) => n.x)) + COLUMN : 0, y: 0 };
    // a meeting sits the protocol's roles and reads its inputs (the catalog's); a bundle box runs its bundle
    const meeting = catalog.value.meetings[0];
    const spec: NodeSpec = "bundle" in item ? { workflow: item.bundle } : type === "meeting" && meeting ? { role: [...meeting.roles], inputs: [...meeting.inputs] } : {};
    if (edit((g) => addNode(g, type, at.x, at.y, spec))) {
      selected.value = id;
      selectedEdge.value = null;
    }
  }

  /** Box `id`'s output `old` called `to`, its lines and readers following (graph.ts `renameOutput`, told the
   * fields of the draft's prompt files); a reader through a prompt file that names it refuses it, shown as the
   * error. True once renamed. */
  function renameOutputOf(id: string, old: string, to: string): boolean {
    return edit((g) => {
      const done = renameOutput(g, id, old, to, promptFieldsOf(prompts.value));
      if ("refused" in done) throw new Error(`${old} cannot be renamed: ${done.refused.join(", ")} read it through a prompt file`);
      return done;
    });
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

  /** Save the canvas as draft `as` serving `type` (a file's name: an edit of that file). Resolves to true once
   * saved; to `{ refused }` with the server's reason when it refuses the name or the text (422: reserved, or the
   * YAML does not read; 409 kept for an older server), for the name dialog to ask again; false on any other failure (shown as the error). An
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
      if (e instanceof ApiError) {
        const users = usedBy(e);
        if (users) return { refused: usedBySentence(as, users) };
        if (e.status === 409 || e.status === 422) return { refused: e.message };
      }
      error.value = e instanceof Error ? e : String(e);
      return false;
    } finally {
      saving.value = false;
    }
  }

  /** Delete draft `as`. Resolves to true once gone; to `{ refused }` with the workflows that use it when the server
   * answers 409; false on any other failure (shown as the error). */
  async function remove(as: string): Promise<true | { refused: string[] } | false> {
    try {
      await deleteDraft(as);
      error.value = null;
      return true;
    } catch (e) {
      const users = e instanceof ApiError ? usedBy(e) : null;
      if (users) {
        error.value = usedBySentence(as, users); // replaces whatever was shown before
        return { refused: users };
      }
      error.value = e instanceof Error ? e : String(e);
      return false;
    }
  }

  /** Open the saved draft's PR. Resolves to true once opened (the definition's `pr` set to it); false when the
   * server refused: its problems (422) shown on the canvas, nothing to publish (409) said as `publishStatus`, any
   * other failure (502 the publisher, 404 the draft gone) shown as the error. */
  async function publish(): Promise<boolean> {
    const as = detail.value?.name ?? name.value;
    const mine = loading; // an answer after another workflow loaded is that one's no longer: it changes nothing
    publishing.value = true;
    publishStatus.value = null;
    try {
      const pr = await publishWorkflow(as);
      if (mine !== loading) return false;
      if (detail.value) detail.value = { ...detail.value, pr };
      error.value = null;
      return true;
    } catch (e) {
      if (mine !== loading) return false;
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
      if (mine === loading) publishing.value = false;
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
    byEdge,
    own,
    upgraded,
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
    restore,
    edit,
    place,
    renameOutput: renameOutputOf,
    remove,
    validate,
    save,
    publish,
  };
}

/** What a 409 naming the workflows that run draft `name` says to a person. */
function usedBySentence(name: string, users: string[]): string {
  return `${name} is used by ${users.join(", ")}: take it out of those first`;
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
