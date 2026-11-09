<script setup lang="ts">
import { NODE_TYPES } from "./graph";
import type { NodeType } from "./graph";
import { SHAPE_CLASS, shapeOf } from "./shapes";

defineOptions({ name: "AirlieBuilderPalette" });

/** The node types in their shapes, then the bundles (the other workflows) by name. Each drags onto the canvas,
 * which places it where it lands (a bundle as a bundle box running it); a click adds one where the canvas has
 * room, for a keyboard or a phone, where nothing drags. */
const props = defineProps<{ bundles: string[]; failed?: boolean; readonly?: boolean }>();
/** A bundle box is placed from the bundles below, which name the workflow it runs. */
const TYPES = NODE_TYPES.filter((t) => t !== "bundle");
const emit = defineEmits<{ retry: []; add: [item: { type: NodeType } | { bundle: string }] }>();

function carry(e: DragEvent, kind: "node" | "bundle", value: string) {
  if (props.readonly || !e.dataTransfer) return;
  e.dataTransfer.setData(`application/x-airlie-${kind}`, value);
  e.dataTransfer.effectAllowed = "copy";
}
function add(item: { type: NodeType } | { bundle: string }) {
  if (!props.readonly) emit("add", item);
}
</script>

<template>
  <aside aria-label="Palette" class="flex flex-col gap-3 rounded-xl border border-border bg-card p-3 shadow-sm min-[901px]:min-h-0 min-[901px]:overflow-auto">
    <h2 class="text-xs font-semibold tracking-[0.06em] text-muted-foreground uppercase">Nodes</h2>
    <ul class="grid grid-cols-2 gap-2 max-[900px]:grid-cols-4 max-[640px]:grid-cols-2">
      <li v-for="t in TYPES" :key="t" class="contents">
        <button
          type="button"
          :data-palette-type="t"
          :data-shape="shapeOf(t)"
          :draggable="readonly ? 'false' : 'true'"
          :title="readonly ? t : `Drag onto the canvas, or click to add a ${t} box`"
          :class="['truncate text-left text-xs font-semibold', SHAPE_CLASS[shapeOf(t)], readonly ? 'cursor-default' : 'cursor-grab']"
          @dragstart="carry($event, 'node', t)"
          @click="add({ type: t })"
        >
          {{ t }}
        </button>
      </li>
    </ul>
    <h2 class="text-xs font-semibold tracking-[0.06em] text-muted-foreground uppercase">Bundles</h2>
    <div data-bundles class="text-[13px]">
      <p v-if="failed" class="text-muted-foreground">
        The workflows could not be loaded.
        <button type="button" class="underline" @click="emit('retry')">Retry</button>
      </p>
      <p v-else-if="!bundles.length" class="text-muted-foreground">None yet</p>
      <ul v-else class="flex flex-col gap-1">
        <li v-for="b in bundles" :key="b" class="contents">
          <button
            type="button"
            data-bundle
            :draggable="readonly ? 'false' : 'true'"
            :title="readonly ? b : `Drag onto the canvas, or click, to run ${b} for each item`"
            :class="['truncate rounded-md border border-border bg-muted/60 px-2 py-1 text-left font-mono text-xs', readonly ? 'cursor-default' : 'cursor-grab']"
            @dragstart="carry($event, 'bundle', b)"
            @click="add({ bundle: b })"
          >
            {{ b }}
          </button>
        </li>
      </ul>
    </div>
  </aside>
</template>
