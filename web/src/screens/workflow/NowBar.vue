<script setup lang="ts">
import { useIntervalFn } from "@vueuse/core";
import { computed, ref } from "vue";
import type { FlowNow } from "@/api/workflows";
import { briefSpan } from "./format";
import { DOT } from "./style";

/** Where the story is right now: the running step's path and its inner step. Quiet time is only said, never colored. */
const props = defineProps<{ now: FlowNow }>();
const clock = ref(Date.now());
useIntervalFn(() => (clock.value = Date.now()), 1000);
const secs = computed(() => clock.value / 1000);

const running = computed(() => props.now.status === "running");
const stepText = computed(() => {
  const n = props.now;
  const parts = [n.steps ? `step ${n.steps}` : null, n.started ? briefSpan(Math.max(0, secs.value - n.started)) : null].filter(Boolean);
  return parts.join(" · ");
});
const quiet = computed(() => (props.now.last_event ? `last event ${Math.max(0, Math.round(secs.value - props.now.last_event))}s ago` : ""));
</script>

<template>
  <div data-now :data-now-status="now.status" class="flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-border px-[18px] py-2 text-[13px]">
    <template v-if="running">
      <span :class="['size-2 flex-none rounded-full', DOT.running, 'motion-safe:animate-pulse']"></span>
      <b class="font-semibold">Running</b>
      <span>{{ now.path.join(" › ") }}</span>
      <span v-if="now.inner" class="font-mono text-xs">{{ now.inner }}</span>
      <span v-if="stepText" class="font-mono text-xs text-muted-foreground">{{ stepText }}</span>
      <span v-if="quiet" class="ml-auto text-xs text-muted-foreground">{{ quiet }}</span>
    </template>
    <template v-else>
      <span :class="['size-2 flex-none rounded-full', now.status === 'stopped' ? DOT.stopped : DOT.pending]"></span>
      <span>{{ now.text }}</span>
    </template>
  </div>
</template>
