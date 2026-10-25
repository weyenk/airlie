<script setup lang="ts">
import { computed } from "vue";
import type { FlowJob } from "@/api/workflows";
import { briefSpan } from "./format";
import { BOX } from "./style";

const props = defineProps<{ nodeId: string; job: FlowJob; selected?: boolean; narrow?: boolean }>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();

const sub = computed(() => [props.job.role, props.job.duration_s ? briefSpan(props.job.duration_s) : null].filter(Boolean).join(" · ") || props.job.status);
</script>

<template>
  <button
    type="button"
    :data-node="nodeId"
    :data-job="job.id"
    :data-status="job.status"
    :aria-pressed="selected ? 'true' : 'false'"
    :class="[
      'flex min-w-0 flex-none flex-col gap-[3px] rounded-[10px] border bg-card text-left outline-none focus-visible:ring-2 focus-visible:ring-ring',
      narrow ? 'w-[132px] px-2.5 py-[7px]' : 'w-32 px-[11px] py-[9px]',
      BOX[job.status],
      selected ? 'ring-2 ring-ring' : '',
      job.status === 'running' ? 'motion-safe:animate-pulse' : '',
    ]"
    @click="emit('pick', nodeId, job.id)"
  >
    <span class="flex w-full min-w-0 items-center gap-1.5 text-[13px] font-semibold">
      <span class="truncate">{{ job.label }}</span>
      <span v-if="job.won" data-won class="shrink-0 text-xs font-bold text-warn-foreground">★ won</span>
      <span v-if="job.attempts > 1" class="shrink-0 rounded-md bg-warn px-1.5 py-px text-xs font-bold text-warn-foreground">{{ job.attempts }}x</span>
    </span>
    <span :class="['w-full truncate text-xs text-muted-foreground', job.role || job.duration_s ? 'font-mono' : '']">{{ sub }}</span>
  </button>
</template>
