<script setup lang="ts">
import { RouterLink } from "vue-router";
import type { WorkflowDef } from "@/api/workflow_defs";
import EmptyState from "@/components/EmptyState.vue";

/** The workflow definitions, files and drafts, each opening in the builder. A draft named as a file is that file's
 * edit (`superseded`: the file runs until the edit merges and the serve reloads), listed after it as such. */
defineProps<{ defs: WorkflowDef[]; failed: boolean }>();
const emit = defineEmits<{ retry: [] }>();

const href = (d: WorkflowDef) => `/workflows/build/${encodeURIComponent(d.name)}`;
const GRID = "grid grid-cols-[minmax(0,1fr)_120px_90px_90px_90px] gap-x-4 max-[640px]:grid-cols-[minmax(0,1fr)_auto_auto]";
</script>

<template>
  <section aria-label="Definitions" class="rounded-xl border border-border bg-card shadow-sm">
    <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
      <h2 class="text-[15px] font-semibold">Definitions</h2>
      <span class="rounded-full bg-muted px-2 py-px text-xs font-semibold text-muted-foreground">{{ defs.length }}</span>
    </div>
    <div v-if="failed" class="flex items-center gap-2 px-[18px] py-3 text-[13px] text-muted-foreground">
      The workflows could not be loaded.
      <button type="button" data-defs-retry class="underline" @click="emit('retry')">Retry</button>
    </div>
    <template v-else>
      <div role="row" :class="[GRID, 'border-b border-border px-[18px] py-2.5 text-xs font-semibold text-muted-foreground']">
        <span>Name</span><span>Type</span><span class="max-[640px]:hidden">Source</span><span>Problems</span><span class="max-[640px]:hidden">PR</span>
      </div>
      <EmptyState v-if="!defs.length" text="No workflows yet" />
      <div v-for="d in defs" :key="`${d.source}:${d.name}`" :data-def="d.name" :data-def-edit="d.superseded ? '' : undefined" :class="[GRID, 'items-center border-b border-border px-[18px] py-[11px] text-sm last:border-b-0']">
        <RouterLink :to="href(d)" class="min-w-0 truncate font-mono hover:underline">{{ d.name }}</RouterLink>
        <span class="text-muted-foreground">{{ d.type ?? "bundle" }}</span>
        <span class="text-muted-foreground max-[640px]:hidden">{{ d.superseded ? `edit of ${d.name}` : d.source }}</span>
        <span data-def-problems :class="d.problems.length ? 'font-semibold text-danger-foreground' : 'text-muted-foreground'" :title="d.problems.join('; ') || undefined">{{ d.problems.length }}</span>
        <span class="max-[640px]:hidden">
          <a v-if="d.pr" :href="d.pr.url" target="_blank" rel="noopener" class="text-muted-foreground underline">#{{ d.pr.number }}</a>
        </span>
      </div>
    </template>
  </section>
</template>
