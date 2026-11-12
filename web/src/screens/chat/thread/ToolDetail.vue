<script setup lang="ts">
import type { ToolUIPart } from "ai";
import { computed } from "vue";
import { inputOf, outputText, PHASE_TEXT, phaseOf, textOfValue, toolName } from "./tool";

/** One call in full: its input as key and value, its output, its state. In the hover card and the phone's sheet. */
const props = defineProps<{ part: ToolUIPart; interrupted?: boolean }>();
const phase = computed(() => phaseOf(props.part, props.interrupted ?? false));
const entries = computed(() => Object.entries(inputOf(props.part)));
const output = computed(() => outputText(props.part));
</script>

<template>
  <div data-detail class="flex flex-col gap-2 text-[13px]">
    <h3 class="m-0 flex items-center gap-2 text-sm font-semibold">
      <span class="font-mono">{{ toolName(part) }}</span>
      <span :class="['rounded-md px-1.5 py-px text-xs font-semibold', phase === 'done' ? 'bg-ok text-ok-foreground' : phase === 'failed' ? 'bg-danger text-danger-foreground' : 'bg-chip text-chip-foreground']">{{ PHASE_TEXT[phase] }}</span>
    </h3>
    <dl v-if="entries.length" class="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
      <template v-for="[key, value] in entries" :key="key">
        <dt class="text-muted-foreground">{{ key }}</dt>
        <dd class="m-0 max-h-24 overflow-y-auto font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]">{{ textOfValue(value) }}</dd>
      </template>
    </dl>
    <pre v-if="output" class="m-0 max-h-48 overflow-y-auto rounded-md border border-border bg-code p-2 font-mono text-xs leading-normal whitespace-pre-wrap [overflow-wrap:anywhere]">{{ output }}</pre>
  </div>
</template>
