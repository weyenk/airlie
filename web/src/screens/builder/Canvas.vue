<script setup lang="ts">
import { computed, useId } from "vue";
import BuilderBox from "./BuilderBox.vue";
import type { GEdge, Graph } from "./graph";

defineOptions({ name: "AirlieBuilderCanvas" });

/** The workflow drawn: a box per node where the graph places it, and one SVG layer of wires under them. Wires
 * leave a box's right edge and enter the next one's left edge; a loop runs below the boxes, dashed. */
const props = defineProps<{ graph: Graph; selected: string | null; readonly?: boolean }>();
const emit = defineEmits<{ select: [id: string]; move: [id: string, x: number, y: number] }>();

const PAD = 24;
const W = 120;
const H = 56;
const marker = `wire-${useId()}`;

const at = computed(() => new Map(props.graph.nodes.map((n) => [n.id, { x: n.x + PAD, y: n.y + PAD }])));
const size = computed(() => {
  const xs = props.graph.nodes.map((n) => n.x);
  const ys = props.graph.nodes.map((n) => n.y);
  const loops = props.graph.edges.some((e) => e.kind === "loop") ? 32 : 0;
  return { width: Math.max(0, ...xs) + W + 2 * PAD, height: Math.max(0, ...ys) + H + 2 * PAD + loops };
});

type Wire = { key: string; edge: GEdge; d: string; label?: { x: number; y: number; text: string } };
const wires = computed<Wire[]>(() =>
  props.graph.edges.flatMap((edge, i) => {
    const a = at.value.get(edge.from);
    const b = at.value.get(edge.to);
    if (!a || !b) return [];
    const sx = a.x + W;
    const sy = a.y + H / 2;
    const tx = b.x;
    const ty = b.y + H / 2;
    let d: string;
    if (edge.kind === "loop") {
      const low = Math.max(a.y, b.y) + H + 16;
      d = `M ${sx} ${sy} H ${sx + 12} V ${low} H ${tx - 12} V ${ty} H ${tx}`;
    } else {
      const dx = Math.max(24, Math.abs(tx - sx) / 2);
      d = `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`;
    }
    const label = edge.kind === "branch" && edge.label !== undefined ? { x: (sx + tx) / 2, y: (sy + ty) / 2 - 5, text: edge.label } : undefined;
    return [{ key: `${i}-${edge.kind}-${edge.from}-${edge.to}`, edge, d, label }];
  }),
);
</script>

<template>
  <div
    data-canvas
    :data-readonly="readonly ? 'true' : undefined"
    class="relative min-w-full bg-[radial-gradient(var(--border)_1px,transparent_1px)] [background-size:16px_16px]"
    :style="{ width: `${size.width}px`, height: `${size.height}px` }"
  >
    <svg :width="size.width" :height="size.height" class="pointer-events-none absolute inset-0 text-muted-foreground" aria-hidden="true">
      <defs>
        <marker :id="marker" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 0 L 8 4 L 0 8 z" fill="currentColor" />
        </marker>
      </defs>
      <g v-for="w in wires" :key="w.key">
        <path
          :data-edge="w.edge.kind"
          :data-from="w.edge.from"
          :data-to="w.edge.to"
          :d="w.d"
          fill="none"
          stroke="currentColor"
          :stroke-width="w.edge.kind === 'parallel' ? 1 : 1.5"
          :stroke-dasharray="w.edge.kind === 'loop' ? '5 4' : undefined"
          :marker-end="`url(#${marker})`"
        />
        <text v-if="w.label" data-edge-label :x="w.label.x" :y="w.label.y" text-anchor="middle" class="fill-current font-mono text-[11px]">{{ w.label.text }}</text>
      </g>
    </svg>
    <BuilderBox
      v-for="n in graph.nodes"
      :key="n.id"
      :node="n"
      :selected="selected === n.id"
      :style="{ left: `${n.x + PAD}px`, top: `${n.y + PAD}px`, width: `${W}px`, height: `${H}px` }"
      @pick="(id) => emit('select', id)"
    />
    <p v-if="!graph.nodes.length" class="absolute top-6 left-6 text-[13px] text-muted-foreground">No boxes yet.</p>
  </div>
</template>
