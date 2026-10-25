<script setup lang="ts">
import { reactive } from "vue";
import type { FlowStep } from "@/api/workflows";
import StatusChip from "@/components/StatusChip.vue";

defineProps<{ steps: FlowStep[] }>();

// The bodies are built only for the steps that are open (a job can have hundreds). What is open is kept here,
// by step, so a live refresh that brings new step objects does not close what a reader opened.
const opened = reactive(new Set<string>());
const id = (s: FlowStep) => `${s.attempt}-${s.n}`;
function toggled(e: Event, s: FlowStep) {
  if ((e.target as HTMLDetailsElement).open) opened.add(id(s));
  else opened.delete(id(s));
}

/** A step's arguments on one line: a string as it is, anything else as JSON. */
const args = (a: unknown): string => (a === null || a === undefined ? "" : typeof a === "string" ? a : JSON.stringify(a));
</script>

<template>
  <div class="flex flex-col overflow-hidden rounded-[10px] border border-border">
    <details v-for="s in steps" :key="id(s)" data-step :open="opened.has(id(s))" class="border-b border-border last:border-b-0" @toggle="toggled($event, s)">
      <summary
        class="grid cursor-pointer grid-cols-[34px_110px_1fr_auto] items-center gap-2.5 px-3 py-2 text-[13px] max-[640px]:grid-cols-[26px_1fr_auto]"
      >
        <span class="font-mono text-muted-foreground">{{ s.attempt ? `r${s.attempt}·${s.n}` : s.n }}</span>
        <span class="truncate font-mono">{{ s.tool ?? "(no tool)" }}</span>
        <span class="truncate font-mono text-xs text-muted-foreground max-[640px]:hidden" :title="args(s.arguments)">{{ args(s.arguments) }}</span>
        <StatusChip v-if="s.ok === false" kind="job" value="failed" />
        <span v-else-if="s.ok" class="rounded-md bg-ok px-2 py-0.5 text-xs font-bold text-ok-foreground">ok</span>
        <span v-else />
      </summary>
      <div v-if="opened.has(id(s))" data-step-body class="flex flex-col gap-1.5 px-3 pb-3 pl-14 max-[640px]:pl-3">
        <pre v-if="s.reasoning" class="whitespace-pre-wrap rounded-lg border border-border bg-code px-3 py-2.5 font-mono text-xs leading-normal">thinking
{{ s.reasoning }}</pre>
        <pre v-if="s.assistant_text" class="whitespace-pre-wrap rounded-lg border border-border bg-code px-3 py-2.5 font-mono text-xs leading-normal">{{ s.assistant_text }}</pre>
        <pre class="whitespace-pre-wrap rounded-lg border border-border bg-code px-3 py-2.5 font-mono text-xs leading-normal">{{ s.observation }}</pre>
      </div>
    </details>
  </div>
</template>
