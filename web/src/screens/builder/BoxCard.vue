<script setup lang="ts">
import type { GNode } from "./graph";

/** The small card a selected box shows beside itself: its name and type, and Edit, which opens the box's fields. */
defineProps<{ node: GNode }>();
const emit = defineEmits<{ edit: [] }>();
</script>

<template>
  <div
    data-box-card
    :data-node="node.id"
    class="flex items-center gap-2 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs shadow-md"
    @pointerdown.stop
    @click.stop
    @dblclick.stop
  >
    <span class="min-w-0">
      <span class="block max-w-[140px] truncate font-semibold">{{ node.id }}</span>
      <span class="block font-mono text-[11px] text-muted-foreground">{{ node.type }}</span>
    </span>
    <button type="button" data-box-edit class="rounded-md border border-border px-2 py-0.5 text-xs font-medium hover:bg-muted" @click="emit('edit')">Edit</button>
  </div>
</template>
