<script setup lang="ts">
import { computed } from "vue";

/** The context the chat has used of what its engine holds, from the last reply's usage: nothing until there is one,
 * and a warning from 90 % of the limit. */
const props = defineProps<{ used: number | null; limit: number | null }>();

const WARN_AT = 0.9;
const k = (tokens: number): string => `${Math.round(tokens / 1000)}k`;
const shown = computed(() => (props.used !== null && props.limit ? { used: props.used, limit: props.limit } : null));
const warn = computed(() => shown.value !== null && shown.value.used >= shown.value.limit * WARN_AT);
const pct = computed(() => (shown.value ? Math.min(100, (shown.value.used / shown.value.limit) * 100) : 0));
</script>

<template>
  <div v-if="shown" data-meter :data-warn="warn ? 'true' : undefined" class="flex items-center gap-1.5 text-xs whitespace-nowrap">
    <span :class="warn ? 'font-semibold text-warn-foreground' : 'text-muted-foreground'">{{ k(shown.used) }} / {{ k(shown.limit) }}</span>
    <div
      role="progressbar"
      aria-label="Context used"
      aria-valuemin="0"
      :aria-valuenow="shown.used"
      :aria-valuemax="shown.limit"
      :aria-valuetext="`${k(shown.used)} of ${k(shown.limit)} tokens${warn ? ', nearly full' : ''}`"
      class="h-1 w-10 overflow-hidden rounded-full bg-track max-[900px]:hidden"
    >
      <i :class="['block h-full rounded-full', warn ? 'bg-warn-foreground' : 'bg-primary']" :style="{ width: `${pct}%` }" />
    </div>
  </div>
</template>
