<script setup lang="ts">
import { computed } from "vue";

// null is "working, no number": a segment slides along the track
const props = defineProps<{ value: number | null }>();
const pct = computed(() => Math.min(100, Math.max(0, props.value ?? 0)));
</script>

<template>
  <div
    role="progressbar"
    aria-valuemin="0"
    aria-valuemax="100"
    :aria-valuenow="value === null ? undefined : pct"
    class="h-1.5 overflow-hidden rounded-full bg-track"
  >
    <i
      v-if="value === null"
      data-indeterminate
      class="block h-full w-[35%] animate-[airlie-slide_1.6s_ease-in-out_infinite] rounded-full bg-primary"
    />
    <i v-else class="block h-full rounded-full bg-primary" :style="{ width: `${pct}%` }" />
  </div>
</template>
