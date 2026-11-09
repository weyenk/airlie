<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, useTemplateRef } from "vue";
import BoxCard from "./BoxCard.vue";
import BuilderBox from "./BuilderBox.vue";
import { inputsOf, outputsOf } from "./graph";
import type { GEdge, GNode, Graph, LineOpts, NodeType } from "./graph";

defineOptions({ name: "AirlieBuilderCanvas" });

/** The workflow drawn: a box per node where the graph places it, and one SVG layer of wires under them. Wires
 * leave a box's right edge and enter the next one's left edge; a loop runs below the boxes, dashed.
 *
 * Editable (not `readonly`): a box drags to a new place (`move`, on an 8 px grid); a wire dragged from a box's
 * right port and let go over another box asks to `connect` them; a click on a wire selects it (`selectEdge`); a
 * node type or a bundle dropped from the palette asks to be placed where it lands (`place`). */
const props = defineProps<{
  graph: Graph;
  selected: string | null;
  selectedEdge?: GEdge | null;
  /** What validation says of each box, by node id. */
  problems?: Record<string, string[]>;
  readonly?: boolean;
}>();
const emit = defineEmits<{
  select: [id: string];
  move: [id: string, x: number, y: number];
  /** A line asked for: `opts.artifacts` is the out-port it was drawn from; none from the edge port (an ordering line). */
  connect: [from: string, to: string, opts: LineOpts];
  selectEdge: [edge: GEdge];
  /** A box opened for editing (double-click, or Edit on its card). */
  open: [id: string];
  openEdge: [edge: GEdge];
  place: [item: { type: NodeType } | { bundle: string }, x: number, y: number];
}>();

const PAD = 24;
const W = 120;
const H = 56;
const GRID = 8;
const marker = `wire-${useId()}`;
const root = useTemplateRef<HTMLDivElement>("root");

const snap = (v: number) => Math.max(0, Math.round(v / GRID) * GRID);

// a box being dragged sits where the pointer has it until it is let go
type Drag = { id: string; px: number; py: number; x0: number; y0: number; x: number; y: number; moved: boolean };
const drag = ref<Drag | null>(null);
let swallowClick = false;

const placeOf = (id: string, x: number, y: number) => (drag.value?.id === id ? { x: drag.value.x, y: drag.value.y } : { x, y });
const at = computed(() => new Map(props.graph.nodes.map((n) => [n.id, ((p) => ({ x: p.x + PAD, y: p.y + PAD }))(placeOf(n.id, n.x, n.y))])));

type Port = { artifact: string; label: string };
/** A box's out-ports: one per output it declares (a bundle box's `name.{item}` when it runs once per item; the
 * bundle workflow's declared outputs when the box declares none). The edge port is drawn after them. */
function portsOf(n: GNode): Port[] {
  const each = n.type === "bundle" && !!n.spec.each;
  const named = outputsOf(n.spec);
  if (named.length) return named.map((artifact) => ({ artifact, label: each ? `${artifact}.{item}` : artifact }));
  const run = props.graph.bundles[typeof n.spec.workflow === "string" ? n.spec.workflow : ""];
  return (run?.outputs ?? []).map((o) => ({ artifact: o.replace(/\.\{item\}$/, ""), label: o }));
}
const PORT_STEP = 16;
const ports = computed(() => new Map(props.graph.nodes.map((n) => [n.id, portsOf(n)])));
/** A box is taller when it has more ports than fit down its right edge. */
const hOf = (id: string) => Math.max(H, 12 + PORT_STEP * ((ports.value.get(id)?.length ?? 0) + 1));
/** Where a port sits down a box's right edge: the outputs in order, the edge port last. */
const portY = (id: string, artifact: string | null) => {
  const list = ports.value.get(id) ?? [];
  const i = artifact === null ? list.length : Math.max(0, list.findIndex((p) => p.artifact === artifact));
  const n = list.length + 1;
  return (at.value.get(id)?.y ?? 0) + hOf(id) / 2 + (i - (n - 1) / 2) * PORT_STEP;
};
const size = computed(() => {
  const loops = props.graph.edges.some((e) => e.loop) ? 32 : 0;
  // room to drop past the last box when editing
  const room = props.readonly ? 0 : 160;
  const pts = props.graph.nodes.map((n) => at.value.get(n.id)!);
  const bottom = Math.max(0, ...props.graph.nodes.map((n) => at.value.get(n.id)!.y - PAD + hOf(n.id)));
  return { width: Math.max(0, ...pts.map((p) => p.x - PAD)) + W + 2 * PAD + room, height: bottom + 2 * PAD + loops + room / 2 };
});

type Wire = { key: string; edge: GEdge; d: string; selected: boolean; label: { x: number; y: number; text: string } };
const same = (a: GEdge, b: GEdge | null | undefined) => !!b && a.from === b.from && a.to === b.to && a.when === b.when;
/** What a line says: the condition first, then what it carries (or "order"), then its loop's bound. */
const labelOf = (edge: GEdge) =>
  [edge.when !== undefined ? `if ${edge.when}` : "", edge.artifacts.join(", ") || "order", edge.loop ? `loop ×${edge.loop.max}` : ""].filter(Boolean).join(" · ");
const wires = computed<Wire[]>(() =>
  props.graph.edges.flatMap((edge, i) => {
    const a = at.value.get(edge.from);
    const b = at.value.get(edge.to);
    if (!a || !b) return [];
    const sx = a.x + W;
    const sy = portY(edge.from, edge.artifacts[0] ?? null);
    const tx = b.x;
    const ty = b.y + hOf(edge.to) / 2;
    let d: string;
    let label: { x: number; y: number };
    if (edge.loop) {
      const low = Math.max(a.y + hOf(edge.from), b.y + hOf(edge.to)) + 16;
      d = `M ${sx} ${sy} H ${sx + 12} V ${low} H ${tx - 12} V ${ty} H ${tx}`;
      label = { x: (sx + tx) / 2, y: low - 5 };
    } else {
      const dx = Math.max(24, Math.abs(tx - sx) / 2);
      d = `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`;
      label = { x: (sx + tx) / 2, y: (sy + ty) / 2 - 5 };
    }
    return [{ key: `${i}-${edge.from}-${edge.to}`, edge, d, selected: same(edge, props.selectedEdge), label: { ...label, text: labelOf(edge) } }];
  }),
);

/** An in-port's hover: what the box reads; a bundle box's, what its workflow is handed. */
function inTitle(n: GNode): string {
  const reads = inputsOf(n.spec);
  if (reads.length) return `reads ${reads.join(", ")}`;
  const run = props.graph.bundles[typeof n.spec.workflow === "string" ? n.spec.workflow : ""];
  return run?.inputs.length ? `takes ${run.inputs.join(", ")}` : "reads nothing";
}

/** A client point as a point on the canvas (the canvas's own pixels, margin included). */
function local(e: { clientX: number; clientY: number }) {
  const r = root.value?.getBoundingClientRect();
  return { x: e.clientX - (r?.left ?? 0), y: e.clientY - (r?.top ?? 0) };
}

// a wire being drawn from a box's right port
const draft = ref<{ from: string; artifact: string | null; x: number; y: number } | null>(null);
const draftPath = computed(() => {
  if (!draft.value) return "";
  const a = at.value.get(draft.value.from);
  if (!a) return "";
  const sx = a.x + W;
  const sy = portY(draft.value.from, draft.value.artifact);
  const dx = Math.max(24, Math.abs(draft.value.x - sx) / 2);
  return `M ${sx} ${sy} C ${sx + dx} ${sy}, ${draft.value.x - dx} ${draft.value.y}, ${draft.value.x} ${draft.value.y}`;
});
/** The box under a canvas point; its left port's reach (8 px out) counts. */
function boxAt(x: number, y: number): string | null {
  for (const [id, p] of at.value) if (x >= p.x - 8 && x <= p.x + W && y >= p.y && y <= p.y + hOf(id)) return id;
  return null;
}

function onMove(e: PointerEvent) {
  if (drag.value) {
    const d = drag.value;
    const dx = e.clientX - d.px;
    const dy = e.clientY - d.py;
    if (!d.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    drag.value = { ...d, moved: true, x: snap(d.x0 + dx), y: snap(d.y0 + dy) };
  } else if (draft.value) {
    draft.value = { ...draft.value, ...local(e) };
  }
}
function onUp(e: PointerEvent) {
  if (drag.value) {
    const d = drag.value;
    drag.value = null;
    if (d.moved) {
      // the click a drag ends with is not a pick; one that never comes must not eat the next
      swallowClick = true;
      setTimeout(() => (swallowClick = false), 0);
      if (d.x !== d.x0 || d.y !== d.y0) emit("move", d.id, d.x, d.y);
    }
  } else if (draft.value) {
    const { from, artifact } = draft.value;
    const p = local(e);
    draft.value = null;
    const to = boxAt(p.x, p.y);
    // a box wired to itself is a move check_workflows has no use for: nothing opens
    if (to !== null && to !== from) dropped(from, artifact, to);
  }
  stopListening();
}
function listen() {
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
  window.addEventListener("pointercancel", cancel);
}
function stopListening() {
  window.removeEventListener("pointermove", onMove);
  window.removeEventListener("pointerup", onUp);
  window.removeEventListener("pointercancel", cancel);
}
function cancel() {
  drag.value = null;
  draft.value = null;
  stopListening();
}
onBeforeUnmount(stopListening);

function startDrag(id: string, e: PointerEvent) {
  swallowClick = false;
  if (props.readonly || e.button !== 0) return;
  const n = props.graph.nodes.find((x) => x.id === id);
  if (!n) return;
  drag.value = { id, px: e.clientX, py: e.clientY, x0: n.x, y0: n.y, x: n.x, y: n.y, moved: false };
  listen();
}
/** A wire let go over `to`: a box that already reads what the port carries (or, from the edge port, already has a
 * line from this box) has that line selected, and nothing else happens; otherwise a line is asked for. */
function dropped(from: string, artifact: string | null, to: string) {
  const target = props.graph.nodes.find((n) => n.id === to);
  const line = props.graph.edges.find((e) => e.from === from && e.to === to);
  if (line && target && (artifact === null || inputsOf(target.spec).includes(artifact) || line.artifacts.includes(artifact))) {
    emit("selectEdge", line);
    return;
  }
  emit("connect", from, to, artifact === null ? {} : { artifacts: [artifact] });
}
function startWire(id: string, artifact: string | null, e: PointerEvent) {
  if (props.readonly || e.button !== 0) return;
  draft.value = { from: id, artifact, ...local(e) };
  listen();
}
function pick(id: string) {
  if (swallowClick) {
    swallowClick = false;
    return;
  }
  emit("select", id);
}

const NODE_MIME = "application/x-airlie-node";
const BUNDLE_MIME = "application/x-airlie-bundle";
function onDragOver(e: DragEvent) {
  if (props.readonly) return;
  const types = [...(e.dataTransfer?.types ?? [])];
  if (types.includes(NODE_MIME) || types.includes(BUNDLE_MIME)) e.preventDefault();
}
function onDrop(e: DragEvent) {
  if (props.readonly || !e.dataTransfer) return;
  const type = e.dataTransfer.getData(NODE_MIME);
  const bundle = e.dataTransfer.getData(BUNDLE_MIME);
  if (!type && !bundle) return;
  e.preventDefault();
  const p = local(e);
  const x = snap(p.x - PAD - W / 2);
  const y = snap(p.y - PAD - H / 2);
  emit("place", type ? { type: type as NodeType } : { bundle }, x, y);
}
</script>

<template>
  <div
    ref="root"
    data-canvas
    :data-readonly="readonly ? 'true' : undefined"
    class="relative min-h-full min-w-full touch-none bg-[radial-gradient(var(--border)_1px,transparent_1px)] [background-size:16px_16px] select-none"
    :style="{ width: `${size.width}px`, height: `${size.height}px` }"
    @dragover="onDragOver"
    @drop="onDrop"
  >
    <svg :width="size.width" :height="size.height" class="pointer-events-none absolute inset-0 text-muted-foreground" aria-hidden="true">
      <defs>
        <marker :id="marker" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 0 L 8 4 L 0 8 z" fill="currentColor" />
        </marker>
      </defs>
      <g v-for="w in wires" :key="w.key" :class="w.selected ? 'text-primary' : undefined">
        <path
          :data-edge="w.edge.loop ? 'loop' : 'line'"
          :data-from="w.edge.from"
          :data-to="w.edge.to"
          :data-selected="w.selected ? 'true' : undefined"
          :d="w.d"
          fill="none"
          stroke="currentColor"
          :stroke-width="w.selected ? 2.5 : 1.5"
          :stroke-dasharray="w.edge.loop ? '5 4' : undefined"
          :marker-end="`url(#${marker})`"
        />
        <path
          v-if="!readonly"
          data-edge-hit
          :data-from="w.edge.from"
          :data-to="w.edge.to"
          :d="w.d"
          fill="none"
          stroke="transparent"
          stroke-width="12"
          pointer-events="stroke"
          class="cursor-pointer"
          @click="emit('selectEdge', w.edge)"
          @dblclick="emit('openEdge', w.edge)"
        >
          <title>{{ w.label.text }}: {{ w.edge.from }} → {{ w.edge.to }}</title>
        </path>
        <text data-edge-label :x="w.label.x" :y="w.label.y" text-anchor="middle" class="fill-current font-mono text-[11px]">{{ w.label.text }}</text>
      </g>
      <path v-if="draft" data-wire-draft :d="draftPath" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3 3" class="text-primary" />
    </svg>
    <template v-for="n in graph.nodes" :key="n.id">
      <BuilderBox
        :node="n"
        :selected="selected === n.id"
        :problems="problems?.[n.id]"
        :style="{ left: `${at.get(n.id)!.x}px`, top: `${at.get(n.id)!.y}px`, width: `${W}px`, height: `${hOf(n.id)}px` }"
        :class="readonly ? undefined : drag?.id === n.id && drag.moved ? 'z-10 cursor-grabbing shadow-md' : 'cursor-grab'"
        @pick="pick"
        @open="emit('open', $event)"
        @pointerdown="(e: PointerEvent) => startDrag(n.id, e)"
      />
      <template v-if="!readonly">
        <span
          data-port="in"
          :data-node="n.id"
          :title="inTitle(n)"
          class="absolute size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-primary/60 bg-card"
          :style="{ left: `${at.get(n.id)!.x}px`, top: `${at.get(n.id)!.y + hOf(n.id) / 2}px` }"
        ></span>
        <span
          v-for="p in ports.get(n.id)"
          :key="p.artifact"
          data-port="out"
          :data-node="n.id"
          :data-artifact="p.artifact"
          :title="`Drag to wire ${p.label} from ${n.id} to another box`"
          class="group absolute z-20 flex -translate-x-1/2 -translate-y-1/2 cursor-crosshair items-center text-[10px] leading-none"
          :style="{ left: `${at.get(n.id)!.x + W}px`, top: `${portY(n.id, p.artifact)}px` }"
          @pointerdown.stop.prevent="(e: PointerEvent) => startWire(n.id, p.artifact, e)"
        >
          <span class="size-3 flex-none rounded-full border border-primary bg-card group-hover:bg-primary"></span>
          <span class="pointer-events-none absolute right-full mr-1 max-w-[100px] truncate rounded-sm bg-card/80 px-0.5 font-mono text-muted-foreground">{{ p.label }}</span>
        </span>
        <span
          data-port="edge"
          :data-node="n.id"
          :title="`Drag for an ordering line: ${n.id} runs first, nothing is passed`"
          class="absolute z-20 size-3 -translate-x-1/2 -translate-y-1/2 cursor-crosshair rounded-full border border-dashed border-primary bg-card hover:bg-primary"
          :style="{ left: `${at.get(n.id)!.x + W}px`, top: `${portY(n.id, null)}px` }"
          @pointerdown.stop.prevent="(e: PointerEvent) => startWire(n.id, null, e)"
        ></span>
      </template>
      <BoxCard
        v-if="selected === n.id && !drag?.moved"
        :node="n"
        class="absolute z-30"
        :style="{ left: `${at.get(n.id)!.x}px`, top: `${at.get(n.id)!.y + hOf(n.id) + 6}px` }"
        @edit="emit('open', n.id)"
      />
    </template>
    <p v-if="!graph.nodes.length" class="absolute top-6 left-6 text-[13px] text-muted-foreground">
      No boxes yet.<template v-if="!readonly"> Drag a node type here from the palette.</template>
    </p>
  </div>
</template>
