<script setup lang="ts">
import { useEventListener } from "@vueuse/core";
import { ChevronLeft } from "lucide-vue-next";
import { computed, nextTick, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink, useRoute, useRouter } from "vue-router";
import type { RouteLocationNormalized } from "vue-router";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useCardDraftStore } from "@/stores/card_draft";
import { useWorkflowDefs } from "./board/workflow";
import Canvas from "./builder/Canvas.vue";
import { holdGraph, takeGraph } from "./builder/builder_state";
import { connect,moveNode, removeEdge, removeNode, renameNode, updateEdge, updateNode } from "./builder/graph";
import type { GEdge, LineOpts, LoopBound, NodeSpec } from "./builder/graph";
import NodeModal from "./builder/NodeModal.vue";
import Palette from "./builder/Palette.vue";
import Problems from "./builder/Problems.vue";
import SaveDialog from "./builder/SaveDialog.vue";
import { useBuilder } from "./builder/useBuilder";
import WireModal from "./builder/WireModal.vue";
import WorkflowModal from "./builder/WorkflowModal.vue";

/** The builder (`/workflows/build/<name>`, or `new` for an empty canvas): the palette, the workflow drawn as boxes
 * and wires, a box's fields in a modal (its card's Edit, or a double-click), a wire's in another (its card's Edit,
 * or a double-click), the workflow's own in a third (the title bar's button), and the YAML. Validate and Save draft run the server's checks, so
 * the canvas is judged by the code that judges a hand-written file. */
const route = useRoute();
const router = useRouter();
const name = computed(() => String(route.params.name ?? ""));
const b = useBuilder(name);
const { detail, graph, error, selected, selectedEdge, problems, catalog } = b;
const tab = ref<"diagram" | "yaml">("diagram");

// the address moving off the builder (a link out, a 401 to sign in) leaves the loaded workflow alone; arriving by a
// bundle's breadcrumb (`?back=<token>`) puts the canvas held at the bundle's door back over what loaded
watch(name, async (n) => {
  if (route.name !== "builder") return;
  const back = typeof route.query.back === "string" ? route.query.back : null;
  await b.load(n);
  if (back === null || route.name !== "builder") return;
  const held = takeGraph(back); // missing or expired: the server's copy stands, no error
  if (held) b.restore(held);
  letGo = true;
  try {
    await router.replace({ query: { ...carried(), ...(held?.up ? { from: held.up.from, parent: held.up.parent } : {}) } });
  } finally {
    letGo = false;
  }
}, { immediate: true });

// a bundle opened from here (`?from=<token>&parent=<name>`): the breadcrumb goes back to the workflow it was
// opened from, with its unsaved canvas
const parentName = computed(() => (typeof route.query.parent === "string" && route.query.parent ? route.query.parent : null));
const parentToken = computed(() => (typeof route.query.from === "string" && route.query.from ? route.query.from : null));
async function crumbBack() {
  if (parentName.value === null) return;
  await router.push({ name: "builder", params: { name: parentName.value }, query: { ...carried(), ...(parentToken.value ? { back: parentToken.value } : {}) } });
}
/** The card the person came from (`?card=`) goes with them into a bundle and back. */
const carried = (): Record<string, string> => (typeof route.query.card === "string" ? { card: route.query.card } : {});
/** A bundle box opens the workflow it runs, in place: this canvas is held (edits and all) and the breadcrumb
 * brings it back. */
async function enterBundle(target: string) {
  const up = parentName.value !== null && parentToken.value !== null ? { from: parentToken.value, parent: parentName.value } : undefined;
  const token = holdGraph(name.value, graph.value, b.prompts.value, up);
  if (token === null && b.dirty.value) {
    b.error.value = "The browser could not hold your unsaved edits; save first";
    return;
  }
  letGo = true; // leaving for the bundle with edits is not leaving them behind
  try {
    // this entry carries the token too, so the browser's Back from the bundle restores the canvas like the breadcrumb
    if (token !== null) await router.replace({ query: { ...route.query, back: token } });
    await router.push({ name: "builder", params: { name: target }, query: { ...carried(), ...(token !== null ? { from: token } : {}), parent: name.value } });
  } finally {
    letGo = false;
  }
}

const { defs, failed, reload } = useWorkflowDefs();
const bundles = computed(() => defs.value.map((d) => d.name).filter((n) => n !== name.value));
const cardTypes = computed(() => [...new Set(defs.value.map((d) => d.type).filter((t): t is string => !!t))]);

const title = computed(() => detail.value?.name ?? name.value);
const typeLabel = computed(() => (detail.value ? (detail.value.type ?? "bundle") : null));
const nodeOf = (id: string) => graph.value.nodes.find((n) => n.id === id);

/** The box whose fields the modal shows; null while it is closed. */
const editing = ref<string | null>(null);
const editNode = computed(() => graph.value.nodes.find((n) => n.id === editing.value) ?? null);
watch(name, () => {
  editing.value = null;
});

/** A click on a box selects it (its card shows); editing is the modal's. */
function pick(id: string) {
  selected.value = selected.value === id ? null : id;
  selectedEdge.value = null;
  closeWire();
}

/** Edit on a box's card (and a problem's box): its fields, in the modal. A bundle box's form has an Open button. */
function openBox(id: string) {
  selected.value = id;
  editing.value = id;
  selectedEdge.value = null;
  closeWire();
}
/** A double-click: a bundle box opens the workflow it runs, in place; any other box (or a bundle naming nothing
 * that exists, so its name can be fixed) opens its fields. */
function enterBox(id: string) {
  const node = nodeOf(id);
  const target = node?.type === "bundle" ? node.spec.workflow : undefined;
  if (typeof target === "string" && defs.value.some((d) => d.name === target)) {
    selected.value = id;
    selectedEdge.value = null;
    closeWire();
    void enterBundle(target);
  } else openBox(id);
}

// a wire: a drag draws the line at once (the modal is for what it carries, says and bounds); a click selects it, and
// its card's Edit or a double-click opens the modal, which always reads the line from the graph
const wire = ref<{ from: string; to: string } | null>(null);
const wireEnds = computed(() => (wire.value ? { from: nodeOf(wire.value.from), to: nodeOf(wire.value.to) } : null));
const wireEdge = computed(() => (wire.value ? (graph.value.edges.find((e) => e.from === wire.value!.from && e.to === wire.value!.to) ?? null) : null));
const findEdge = (from: string, to: string) => graph.value.edges.find((e) => e.from === from && e.to === to) ?? null;
/** What the person changed since the last check is checked again, when they have asked for a check before. */
function recheck() {
  if (b.validated.value) void b.validate();
}
function drawn(from: string, to: string, opts: LineOpts = {}) {
  if (!b.edit((g) => connect(g, from, to, opts))) return;
  selected.value = null;
  selectedEdge.value = findEdge(from, to);
  recheck();
}
function clickedWire(edge: GEdge) {
  selected.value = null;
  selectedEdge.value = edge;
}
function openWire(edge: GEdge) {
  clickedWire(edge);
  wire.value = { from: edge.from, to: edge.to };
}
function closeWire() {
  wire.value = null;
}
function applyWire(change: { artifacts: string[]; when?: string; loop?: LoopBound }) {
  const w = wire.value;
  if (!w) return;
  if (b.edit((g) => updateEdge(g, w.from, w.to, change))) {
    selectedEdge.value = findEdge(w.from, w.to);
    recheck();
  }
}
function dropWire() {
  const e = wireEdge.value;
  if (e) b.edit((g) => removeEdge(g, e));
  closeWire();
  selectedEdge.value = null;
  recheck();
}

// the modal's changes (live: the graph holds each as it is made), each to the box it names, so a change that
// lands as the modal closes (editing already null) still reaches its box
function updateBox(spec: NodeSpec, id: string) {
  b.edit((g) => updateNode(g, id, spec));
}
function renameBox(to: string, id: string) {
  if (!b.edit((g) => renameNode(g, id, to))) return;
  if (editing.value === id) editing.value = to;
  if (selected.value === id) selected.value = to;
}
function renameBoxOutput(old: string, to: string, id: string) {
  b.renameOutput(id, old, to);
}
function removeBox(id: string) {
  b.edit((g) => removeNode(g, id));
  editing.value = null;
  selected.value = null;
}
function addPrompt(id: string, text: string) {
  b.prompts.value = { ...b.prompts.value, [id]: text };
}
/** A problem's line, shown: the diagram, the line selected, its modal open. */
function focusEdge(from: string, to: string) {
  const e = findEdge(from, to);
  if (!e) return;
  tab.value = "diagram";
  editing.value = null;
  openWire(e);
}
/** A problem's box, shown: the diagram, the box selected, its fields open. */
function focus(id: string) {
  tab.value = "diagram";
  closeWire();
  selectedEdge.value = null;
  selected.value = id;
  editing.value = id;
}

// saving: in place for a draft, and for a file's workflow (a draft of the file's name is an edit of that file,
// published as a change to it; the file runs until then); a new canvas asks for a name first
const asking = ref(false);
const reason = ref<string | null>(null);
async function saveClick() {
  if (b.isNew.value) {
    reason.value = null;
    asking.value = true;
    return;
  }
  await saveAs(detail.value?.name ?? name.value, graph.value.type);
}
/** The workflow's own fields, in the modal the title bar opens: its type, the inputs its caller supplies and the
 * outputs it hands back. */
const editingWorkflow = ref(false);
function updateWorkflow(change: { type?: string | null; inputs?: string[]; outputs?: string[] }) {
  b.edit((g) => ({ ...g, ...(change.type !== undefined ? { type: change.type } : {}), ...(change.inputs ? { inputs: change.inputs } : {}), ...(change.outputs ? { outputs: change.outputs } : {}) }));
}

// Delete draft: asked first; the draft goes (an edit of a file leaves the file as it is) and the builder returns
// to the Workflows page
const deleting = ref(false);
async function deleteConfirmed() {
  deleting.value = false;
  const gone = detail.value?.name ?? name.value;
  const done = await b.remove(gone);
  if (typeof done === "object") {
    return; // the hook said why (b.error)
  }
  if (!done) return;
  letGo = true; // the draft is gone: its unsaved changes with it
  try {
    await router.push("/workflows");
  } finally {
    letGo = false;
  }
}
async function saveAs(as: string, type: string | null) {
  asking.value = false;
  const done = await b.save(as, type);
  if (typeof done === "object") {
    // a name (or text) the server refused: ask again, saying why
    reason.value = `The server refused ${as}: ${done.refused}`;
    asking.value = true;
  } else if (done && card.value) await backToCard(as);
  else if (done && as !== name.value) await router.replace(`/workflows/build/${encodeURIComponent(as)}`);
}

async function publishClick() {
  const as = detail.value?.name ?? name.value;
  if (!(await b.publish())) return;
  if (card.value) await backToCard(as);
  else void reload(); // the definitions list (the palette's bundles, a card's Workflow field) learns of the PR
}

// from a card (Create's `＋ Build new…`, `?card=<token>`): a save or a publish returns to it with the workflow
// chosen; Cancel returns with the card as it was
const card = computed(() => (typeof route.query.card === "string" ? route.query.card : null));
const drafts = useCardDraftStore();
const backTo = (token: string) => ({ name: "board", query: { create: token } });
async function backToCard(workflow: string) {
  const token = card.value!;
  drafts.setWorkflow(token, workflow);
  await router.push(backTo(token));
}
async function cancel() {
  letGo = true; // an explicit leave: nothing to ask
  try {
    await router.push(backTo(card.value!));
  } finally {
    letGo = false;
  }
}
/** The dialog asks for a new canvas's name, or again after a refusal: a draft's name is taken (saving would replace
 * it); a file's name is not, and saves an edit of that file. */
const taken = computed(() => defs.value.filter((d) => d.source === "draft" && d.name !== name.value).map((d) => d.name));
const files = computed(() => defs.value.filter((d) => d.source === "file").map((d) => d.name));

// unsaved changes: leaving (or loading another workflow) asks first; closing the tab asks the browser's way
const leaving = ref<string | null>(null);
let letGo = false;
function guard(to: RouteLocationNormalized) {
  if (letGo || !b.dirty.value) return true;
  leaving.value = to.fullPath;
  return false;
}
onBeforeRouteLeave(guard);
onBeforeRouteUpdate(guard);
useEventListener(window, "beforeunload", (e: BeforeUnloadEvent) => {
  if (b.dirty.value) e.preventDefault();
});
async function leaveAnyway() {
  const to = leaving.value!;
  leaving.value = null;
  letGo = true;
  try {
    await router.push(to);
  } finally {
    letGo = false;
  }
}

/** A file's workflow as the file has it: saving it would only rewrite the file's text (its comments regenerated
 * away), so Save waits for an edit. */
const untouchedFile = computed(() => detail.value?.source === "file" && !b.dirty.value);

// the canvas pane scrolls when the layout is wider than it: a fade at its right edge says more is off-screen
const pane = ref<HTMLElement | null>(null);
const more = ref(false);
function measure() {
  const el = pane.value;
  more.value = !!el && el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
}
useEventListener(window, "resize", measure);
watch([graph, tab, pane], () => void nextTick(measure), { immediate: true, flush: "post" });

const count = computed(() => problems.value.length);
/** Why Publish is off, as its tooltip; undefined when it is on (a publish in flight aside). */
const publishHelp = computed(() => (b.unsaved.value ? "Save first" : count.value ? "Fix the problems first" : undefined));
const tabClass = (on: boolean) => ["rounded-md px-2.5 py-1 text-[13px] font-medium", on ? "bg-card text-foreground shadow-sm" : "text-muted-foreground"];
</script>

<template>
  <div class="flex flex-col gap-4 px-5 pt-6 pb-10 max-[640px]:gap-3 max-[640px]:px-3 max-[640px]:pt-4 min-[901px]:h-[calc(100dvh-var(--header-h))] min-[901px]:pb-5">
    <section class="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border bg-card px-[18px] py-3.5 shadow-sm">
      <button v-if="parentName" type="button" data-breadcrumb class="inline-flex items-center gap-1 font-mono text-[13px] text-muted-foreground hover:underline" @click="crumbBack"><ChevronLeft class="size-4" />{{ parentName }}</button>
      <RouterLink to="/workflows" class="inline-flex items-center gap-1 text-[13px] text-muted-foreground"><ChevronLeft class="size-4" />Workflows</RouterLink>
      <h1 class="font-mono text-lg font-bold">{{ title }}</h1>
      <span v-if="typeLabel" data-workflow-type class="rounded-full bg-chip px-2 py-px text-xs font-semibold text-chip-foreground">{{ typeLabel }}</span>
      <span v-if="detail?.edits_file" data-edits-file :title="`Publishing changes the repo's ${detail.name}; until it merges, the file runs`" class="rounded-full bg-warn px-2 py-px text-xs font-semibold text-warn-foreground">edit of {{ detail.name }}</span>
      <span v-else-if="detail?.source === 'draft'" class="rounded-full bg-warn px-2 py-px text-xs font-semibold text-warn-foreground">draft</span>
      <span v-if="b.upgraded.value" data-upgraded class="rounded-full bg-warn px-2 py-px text-xs font-semibold text-warn-foreground">upgraded to the current schema, unsaved</span>
      <span v-if="count" data-problems class="rounded-full bg-danger px-2 py-px text-xs font-semibold text-danger-foreground">{{ count }} {{ count === 1 ? "problem" : "problems" }}</span>
      <a v-if="detail?.pr" data-pr :href="detail.pr.url" target="_blank" rel="noopener" class="text-[13px] text-muted-foreground underline">PR #{{ detail.pr.number }}</a>
      <span v-if="b.dirty.value" data-dirty class="text-xs text-muted-foreground">Unsaved changes</span>
      <span class="grow" />
      <Button v-if="card" size="sm" variant="ghost" data-cancel @click="cancel">Cancel</Button>
      <Button v-if="detail?.source === 'draft'" size="sm" variant="ghost" data-delete :disabled="b.saving.value || b.publishing.value" @click="deleting = true">Delete draft</Button>
      <Button size="sm" variant="outline" data-workflow-edit :disabled="b.broken.value" @click="editingWorkflow = true">Workflow</Button>
      <Button size="sm" variant="outline" data-validate :disabled="b.broken.value" @click="b.validate">Validate</Button>
      <Button size="sm" data-save :title="untouchedFile ? 'nothing changed' : undefined" :disabled="b.broken.value || b.saving.value || b.publishing.value || untouchedFile" @click="saveClick">Save draft</Button>
      <Button size="sm" variant="outline" data-publish :title="publishHelp" :disabled="publishHelp !== undefined || b.publishing.value || b.saving.value" @click="publishClick">Publish</Button>
      <div role="tablist" aria-label="View" class="inline-flex gap-0.5 rounded-lg bg-muted p-[3px]">
        <button type="button" role="tab" data-tab="diagram" :aria-selected="tab === 'diagram'" :class="tabClass(tab === 'diagram')" @click="tab = 'diagram'">Diagram</button>
        <button type="button" role="tab" data-tab="yaml" :aria-selected="tab === 'yaml'" :class="tabClass(tab === 'yaml')" @click="tab = 'yaml'">YAML</button>
      </div>
    </section>

    <ErrorBanner v-if="error" :error="error" />
    <p v-if="b.publishStatus.value" data-publish-status role="status" class="text-[13px] text-muted-foreground">{{ b.publishStatus.value }}</p>
    <p v-if="b.catalogFailed.value" data-catalog-failed class="text-[13px] text-muted-foreground">
      The registry's catalog could not be loaded, so the forms offer only what the boxes already have.
      <button type="button" class="underline" @click="b.loadCatalog">Retry</button>
    </p>
    <template v-if="count || b.validated.value">
      <Problems :problems="problems" :workflow="b.own.value" @focus="focus" @focus-edge="focusEdge" />
      <p v-if="b.stale.value" data-problems-stale class="-mt-2 text-xs text-muted-foreground">Edited since this check: Validate again to see where it stands.</p>
    </template>

    <template v-if="tab === 'yaml'">
      <p data-yaml-notice class="text-[13px] text-muted-foreground">
        Saving regenerates the file. A draft's comments are kept only while its definition is unchanged; a file's comments are regenerated away when it is saved as a draft.
      </p>
      <pre data-yaml class="overflow-auto rounded-xl border border-border bg-[var(--code)] p-4 font-mono text-xs leading-relaxed min-[901px]:min-h-0 min-[901px]:flex-1">{{ b.yamlText.value }}</pre>
    </template>

    <div v-else class="grid grid-cols-[180px_minmax(0,1fr)] items-start gap-3 max-[900px]:grid-cols-1 min-[901px]:min-h-0 min-[901px]:flex-1 min-[901px]:items-stretch">
      <Palette :bundles="bundles" :failed="failed" :readonly="b.broken.value" @retry="reload" @add="(item) => b.place(item)" />
      <div class="relative min-w-0 min-[901px]:h-full">
        <section
          ref="pane"
          aria-label="Canvas"
          data-canvas-pane
          class="canvas-pane min-h-[240px] overflow-auto rounded-xl border border-border bg-card shadow-sm max-[900px]:max-h-[calc(100dvh-var(--header-h)-200px)] min-[901px]:h-full"
          @scroll.passive="measure"
        >
          <Canvas
            :graph="graph"
            :selected="selected"
            :selected-edge="selectedEdge"
            :problems="b.byNode.value"
            :edge-problems="b.byEdge.value"
            :readonly="b.broken.value"
            @select="pick"
            @move="(id, x, y) => b.edit((g) => moveNode(g, id, x, y))"
            @connect="drawn"
            @open="openBox"
            @enter="enterBox"
            @select-edge="clickedWire"
            @open-edge="openWire"
            @place="(item, x, y) => b.place(item, x, y)"
          />
        </section>
        <div
          v-if="more"
          data-canvas-more
          aria-hidden="true"
          class="pointer-events-none absolute top-px right-[11px] bottom-[11px] w-12 bg-gradient-to-l from-card to-transparent"
        ></div>
      </div>
    </div>

    <NodeModal
      v-if="editNode"
      :open="true"
      :node="editNode"
      :graph="graph"
      :catalog="catalog"
      :prompts="b.prompts.value"
      :bundles="bundles"
      @update:open="(v) => !v && (editing = null)"
      @update="updateBox"
      @rename="renameBox"
      @rename-output="renameBoxOutput"
      @remove="removeBox"
      @create-prompt="addPrompt"
      @open-bundle="(workflow) => void enterBundle(workflow)"
    />
    <WireModal
      v-if="wire && wireEnds?.from && wireEnds.to"
      :open="true"
      :edge="wireEdge"
      :from="wireEnds.from"
      :to="wireEnds.to"
      :graph="graph"
      @update:open="(v) => !v && closeWire()"
      @apply="applyWire"
      @remove="dropWire"
    />
    <WorkflowModal v-model:open="editingWorkflow" :type="graph.type" :inputs="graph.inputs" :outputs="graph.outputs" :types="cardTypes" @update="updateWorkflow" />
    <SaveDialog v-model:open="asking" :taken="taken" :files="files" :types="cardTypes" :type="graph.type" :reason="reason" @save="saveAs" />
    <Dialog :open="deleting" @update:open="(v) => !v && (deleting = false)">
      <DialogContent :show-close-button="false">
        <DialogHeader>
          <DialogTitle>Delete the draft {{ title }}?</DialogTitle>
          <DialogDescription>{{ detail?.edits_file ? `The edit goes; the repo's ${title} stays as it is.` : "The draft and its prompt files go. A card that names it can no longer start." }}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" data-delete-cancel @click="deleting = false">Keep it</Button>
          <Button variant="destructive" data-delete-confirm @click="deleteConfirmed">Delete draft</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog :open="leaving !== null" @update:open="(v) => !v && (leaving = null)">
      <DialogContent :show-close-button="false">
        <DialogHeader>
          <DialogTitle>Leave without saving?</DialogTitle>
          <DialogDescription>Your changes to {{ title }} are not saved.</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" data-leave-stay @click="leaving = null">Stay</Button>
          <Button variant="destructive" data-leave-go @click="leaveAnyway">Leave without saving</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>

<style scoped>
/* The canvas pane's scrollbars are always drawn (an overlay scrollbar hides that the layout runs on): thin, in the
 * theme's colours. Chromium and Safari draw a styled ::-webkit-scrollbar as a classic one; Firefox reads the
 * standard properties (which Chromium would let override the styled one, so they are Firefox's alone). */
.canvas-pane {
  scrollbar-gutter: stable;
}
.canvas-pane::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}
.canvas-pane::-webkit-scrollbar-track {
  background: var(--muted);
  border-radius: 0 0 12px 12px;
}
.canvas-pane::-webkit-scrollbar-thumb {
  background: var(--input);
  border: 2px solid var(--muted);
  border-radius: 999px;
}
.canvas-pane::-webkit-scrollbar-thumb:hover {
  background: var(--muted-fg);
}
.canvas-pane::-webkit-scrollbar-corner {
  background: var(--muted);
}
@supports not selector(::-webkit-scrollbar) {
  .canvas-pane {
    scrollbar-width: thin;
    scrollbar-color: var(--input) var(--muted);
  }
}
</style>
