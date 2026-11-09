<script setup lang="ts">
import { edgeKey, problemEdge, problemNode } from "./problems";

defineOptions({ name: "AirlieBuilderProblems" });

/** What the composer's checks say of the workflow: a row each; one that names a box is a button that shows it, and one that names a line opens that line. */
defineProps<{ problems: string[] }>();
const emit = defineEmits<{ focus: [nodeId: string]; focusEdge: [from: string, to: string] }>();
</script>

<template>
  <section aria-label="Problems" data-problem-list class="flex flex-col gap-1.5 rounded-xl border border-border bg-card px-3 py-2.5 text-[13px] shadow-sm">
    <p v-if="!problems.length" data-no-problems class="text-muted-foreground">No problems: the composer accepts this workflow.</p>
    <ul v-else class="flex flex-col gap-1">
      <li v-for="(p, i) in problems" :key="i" data-problem class="flex items-start gap-2">
        <span class="mt-1.5 size-1.5 flex-none rounded-full bg-danger" aria-hidden="true"></span>
        <button
          v-if="problemEdge(p)"
          type="button"
          :data-problem-edge="edgeKey(problemEdge(p)!.from, problemEdge(p)!.to)"
          class="text-left font-mono text-xs break-words underline-offset-2 hover:underline"
          @click="emit('focusEdge', problemEdge(p)!.from, problemEdge(p)!.to)"
        >
          {{ p }}
        </button>
        <button
          v-else-if="problemNode(p)"
          type="button"
          :data-problem-node="problemNode(p)"
          class="text-left font-mono text-xs break-words underline-offset-2 hover:underline"
          @click="emit('focus', problemNode(p)!)"
        >
          {{ p }}
        </button>
        <span v-else class="font-mono text-xs break-words">{{ p }}</span>
      </li>
    </ul>
  </section>
</template>
