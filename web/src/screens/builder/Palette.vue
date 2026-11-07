<script setup lang="ts">
import { NODE_TYPES } from "./graph";
import { SHAPE_CLASS, shapeOf } from "./shapes";

defineOptions({ name: "AirlieBuilderPalette" });

/** The node types in their shapes, then the bundles (the other workflows) by name. Read-only for now: placing a
 * box from here comes with editing. */
defineProps<{ bundles: string[]; failed?: boolean }>();
const emit = defineEmits<{ retry: [] }>();
</script>

<template>
  <aside aria-label="Palette" class="flex flex-col gap-3 rounded-xl border border-border bg-card p-3 shadow-sm">
    <h2 class="text-xs font-semibold tracking-[0.06em] text-muted-foreground uppercase">Nodes</h2>
    <ul class="grid grid-cols-2 gap-2 max-[900px]:grid-cols-4 max-[640px]:grid-cols-2">
      <li
        v-for="t in NODE_TYPES"
        :key="t"
        :data-palette-type="t"
        :data-shape="shapeOf(t)"
        :class="['truncate text-xs font-semibold', SHAPE_CLASS[shapeOf(t)]]"
      >
        {{ t }}
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
        <li v-for="b in bundles" :key="b" data-bundle class="truncate rounded-md border border-border bg-muted/60 px-2 py-1 font-mono text-xs">{{ b }}</li>
      </ul>
    </div>
  </aside>
</template>
