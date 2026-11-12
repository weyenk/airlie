<script setup lang="ts">
import { computed } from "vue";
import { linesOf } from "./tool";

/** An `edit_file` (`old` to `new`) or `write_file` (all `new`) as lines with a +/- gutter. Text only: nothing in it is
 * ever parsed as HTML, and the gutter character says added or removed where the tint does not. */
const props = defineProps<{ path: string; old?: string; new: string }>();

const lines = computed(() => [
  ...linesOf(props.old ?? "").map((text) => ({ kind: "removed" as const, mark: "-", text })),
  ...linesOf(props.new).map((text) => ({ kind: "added" as const, mark: "+", text })),
]);
const added = computed(() => lines.value.filter((l) => l.kind === "added").length);
const removed = computed(() => lines.value.length - added.value);
</script>

<template>
  <figure class="m-0 overflow-hidden rounded-lg border border-border bg-code" data-diff>
    <figcaption class="flex items-center justify-between gap-2 border-b border-border px-3 py-1.5 font-mono text-xs [overflow-wrap:anywhere]">
      <span>{{ path }}</span>
      <span class="flex flex-none gap-1.5 font-semibold"><span class="text-ok-foreground">+{{ added }}</span><span class="text-danger-foreground">-{{ removed }}</span></span>
    </figcaption>
    <div class="max-h-80 overflow-y-auto py-1 font-mono text-xs leading-[1.6]">
      <div v-for="(l, i) in lines" :key="i" :data-line="l.kind" :class="['flex', l.kind === 'added' ? 'bg-ok/60' : 'bg-danger/60']">
        <span class="w-6 flex-none text-center select-none"><span class="sr-only">{{ l.kind }}: </span>{{ l.mark }}</span>
        <span class="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{{ l.text }}</span>
      </div>
    </div>
  </figure>
</template>
