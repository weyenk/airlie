<script setup lang="ts">
import { useEventListener } from "@vueuse/core";
import { ChevronLeft } from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink, useRoute, useRouter } from "vue-router";
import type { RouteLocationNormalized } from "vue-router";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useCardDraftStore } from "@/stores/card_draft";
import { useWorkflowDefs } from "./board/workflow";
import Canvas from "./builder/Canvas.vue";
import { canRemove, connect, isChild, moveNode, readable, removeEdge, removeNode, renameNode, updateNode } from "./builder/graph";
import type { GEdge, Move, MoveOpts, NodeSpec } from "./builder/graph";
import NodeForm from "./builder/NodeForm.vue";
import Palette from "./builder/Palette.vue";
import Problems from "./builder/Problems.vue";
import SaveDialog from "./builder/SaveDialog.vue";
import { useBuilder } from "./builder/useBuilder";
import WireForm from "./builder/WireForm.vue";

/** The builder (`/workflows/build/<name>`, or `new` for an empty canvas): the palette, the workflow drawn as boxes
 * and wires, the selected box's or wire's form, and the YAML. Validate and Save draft run the server's checks, so
 * the canvas is judged by the code that judges a hand-written file. */
const route = useRoute();
const router = useRouter();
const name = computed(() => String(route.params.name ?? ""));
const b = useBuilder(name);
const { detail, graph, error, selected, selectedEdge, problems, catalog } = b;
const tab = ref<"diagram" | "yaml">("diagram");

// the address moving off the builder (a link out, a 401 to sign in) leaves the loaded workflow alone
watch(name, (n) => {
  if (route.name === "builder") void b.load(n);
}, { immediate: true });

const { defs, failed, reload } = useWorkflowDefs();
const bundles = computed(() => defs.value.map((d) => d.name).filter((n) => n !== name.value));
const cardTypes = computed(() => [...new Set(defs.value.map((d) => d.type).filter((t): t is string => !!t))]);

const title = computed(() => detail.value?.name ?? name.value);
const typeLabel = computed(() => (detail.value ? (detail.value.type ?? "bundle") : null));
const node = computed(() => graph.value.nodes.find((n) => n.id === selected.value) ?? null);
const nodeOf = (id: string) => graph.value.nodes.find((n) => n.id === id);

function pick(id: string) {
  selected.value = selected.value === id ? null : id;
  selectedEdge.value = null;
  wire.value = null;
}

// a wire: one just drawn (no edge yet) or one clicked
const wire = ref<{ from: string; to: string; edge: GEdge | null } | null>(null);
const wireEnds = computed(() => (wire.value ? { from: nodeOf(wire.value.from), to: nodeOf(wire.value.to) } : null));
function drawn(from: string, to: string) {
  wire.value = { from, to, edge: null };
  selected.value = null;
  selectedEdge.value = null;
}
function clickedWire(edge: GEdge) {
  wire.value = { from: edge.from, to: edge.to, edge };
  selected.value = null;
  selectedEdge.value = edge;
}
function closeWire() {
  wire.value = null;
  selectedEdge.value = null;
}
function applyWire(move: Move, opts: MoveOpts) {
  const w = wire.value!;
  if (b.edit((g) => connect(w.edge ? removeEdge(g, w.edge) : g, w.from, w.to, move, opts))) closeWire();
}
function dropWire() {
  const e = wire.value?.edge;
  if (e) b.edit((g) => removeEdge(g, e));
  closeWire();
}

function updateBox(spec: NodeSpec) {
  const id = selected.value!;
  b.edit((g) => updateNode(g, id, spec));
}
function renameBox(to: string) {
  const id = selected.value!;
  if (b.edit((g) => renameNode(g, id, to))) selected.value = to;
}
function removeBox() {
  const id = selected.value!;
  b.edit((g) => removeNode(g, id));
  selected.value = null;
}
function addPrompt(id: string, text: string) {
  b.prompts.value = { ...b.prompts.value, [id]: text };
}
/** A problem's box, shown: the diagram, the box selected, its form open. */
function focus(id: string) {
  tab.value = "diagram";
  closeWire();
  selected.value = id;
}

// saving: in place for a draft; a new canvas, or a file's workflow, asks for a name first
const asking = ref(false);
const reason = ref<string | null>(null);
async function saveClick() {
  if (b.isNew.value || detail.value?.source === "file") {
    reason.value = detail.value?.source === "file" ? `${detail.value.name} is a workflow in the repo's files; your changes save as a draft under a name of its own.` : null;
    asking.value = true;
    return;
  }
  await saveAs(name.value, graph.value.type);
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
/** The dialog only asks when the current name will not do (a new canvas, a file's name, a name refused), so every
 * name a workflow has is taken. */
const taken = computed(() => defs.value.map((d) => d.name));

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

const count = computed(() => problems.value.length);
/** Why Publish is off, as its tooltip; undefined when it is on (a publish in flight aside). */
const publishHelp = computed(() => (b.unsaved.value ? "Save first" : count.value ? "Fix the problems first" : undefined));
const tabClass = (on: boolean) => ["rounded-md px-2.5 py-1 text-[13px] font-medium", on ? "bg-card text-foreground shadow-sm" : "text-muted-foreground"];
</script>

<template>
  <div class="flex flex-col gap-4 px-5 pt-6 pb-10 max-[640px]:gap-3 max-[640px]:px-3 max-[640px]:pt-4">
    <section class="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border bg-card px-[18px] py-3.5 shadow-sm">
      <RouterLink to="/workflows" class="inline-flex items-center gap-1 text-[13px] text-muted-foreground"><ChevronLeft class="size-4" />Workflows</RouterLink>
      <h1 class="font-mono text-lg font-bold">{{ title }}</h1>
      <span v-if="typeLabel" data-workflow-type class="rounded-full bg-chip px-2 py-px text-xs font-semibold text-chip-foreground">{{ typeLabel }}</span>
      <span v-if="detail?.source === 'draft'" class="rounded-full bg-warn px-2 py-px text-xs font-semibold text-warn-foreground">draft</span>
      <span v-if="count" data-problems class="rounded-full bg-danger px-2 py-px text-xs font-semibold text-danger-foreground">{{ count }} {{ count === 1 ? "problem" : "problems" }}</span>
      <a v-if="detail?.pr" data-pr :href="detail.pr.url" target="_blank" rel="noopener" class="text-[13px] text-muted-foreground underline">PR #{{ detail.pr.number }}</a>
      <span v-if="b.dirty.value" data-dirty class="text-xs text-muted-foreground">Unsaved changes</span>
      <span class="grow" />
      <Button v-if="card" size="sm" variant="ghost" data-cancel @click="cancel">Cancel</Button>
      <Button size="sm" variant="outline" data-validate :disabled="b.broken.value" @click="b.validate">Validate</Button>
      <Button size="sm" data-save :disabled="b.broken.value || b.saving.value || b.publishing.value" @click="saveClick">Save draft</Button>
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
      <Problems :problems="problems" @focus="focus" />
      <p v-if="b.stale.value" data-problems-stale class="-mt-2 text-xs text-muted-foreground">Edited since this check: Validate again to see where it stands.</p>
    </template>

    <template v-if="tab === 'yaml'">
      <p data-yaml-notice class="text-[13px] text-muted-foreground">Saving regenerates the file; comments in a hand-written file are kept only when the definition is unchanged.</p>
      <pre data-yaml class="overflow-auto rounded-xl border border-border bg-[var(--code)] p-4 font-mono text-xs leading-relaxed">{{ b.yamlText.value }}</pre>
    </template>

    <div v-else class="grid grid-cols-[180px_minmax(0,1fr)_280px] items-start gap-3 max-[900px]:grid-cols-1">
      <Palette :bundles="bundles" :failed="failed" :readonly="b.broken.value" @retry="reload" @add="(item) => b.place(item)" />
      <section aria-label="Canvas" class="max-h-[calc(100dvh-var(--header-h)-200px)] min-h-[240px] overflow-auto rounded-xl border border-border bg-card shadow-sm">
        <Canvas
          :graph="graph"
          :selected="selected"
          :selected-edge="selectedEdge"
          :problems="b.byNode.value"
          :readonly="b.broken.value"
          @select="pick"
          @move="(id, x, y) => b.edit((g) => moveNode(g, id, x, y))"
          @connect="drawn"
          @select-edge="clickedWire"
          @place="(item, x, y) => b.place(item, x, y)"
        />
      </section>
      <aside aria-label="Fields" data-fields class="flex flex-col gap-2 rounded-xl border border-border bg-card p-3 text-[13px] shadow-sm">
        <WireForm
          v-if="wire && wireEnds?.from && wireEnds.to"
          :from="wireEnds.from"
          :to="wireEnds.to"
          :existing="wire.edge"
          :removable="wire.edge ? canRemove(graph, wire.edge) : false"
          :bundles="bundles"
          :predicates="catalog.predicates"
          :from-child="isChild(graph, wire.from)"
          @apply="applyWire"
          @remove="dropWire"
          @cancel="closeWire"
        />
        <NodeForm
          v-else-if="node"
          :key="node.id"
          :node="node"
          :catalog="catalog"
          :upstream="readable(graph, node.id)"
          :prompts="b.prompts.value"
          :bundles="bundles"
          @update="updateBox"
          @rename="renameBox"
          @remove="removeBox"
          @create-prompt="addPrompt"
        />
        <template v-else>
          <p class="text-muted-foreground">Select a box or a wire to see its fields. Drag from a box's right port to another box to wire them.</p>
          <dl v-if="graph.start" class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5">
            <dt class="text-muted-foreground">start</dt>
            <dd class="font-mono text-xs">{{ graph.start }}</dd>
            <template v-if="graph.loose.length">
              <dt class="text-muted-foreground">loose</dt>
              <dd class="font-mono text-xs">{{ graph.loose.join(", ") }}</dd>
            </template>
          </dl>
        </template>
      </aside>
    </div>

    <SaveDialog v-model:open="asking" :taken="taken" :types="cardTypes" :type="graph.type" :reason="reason" @save="saveAs" />
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
