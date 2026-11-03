<script setup lang="ts">
import { computed } from "vue";
import type { FlowJob } from "@/api/workflows";
import { BOX, DOT } from "./style";

const props = defineProps<{ nodeId: string; job: FlowJob; selected?: boolean; narrow?: boolean }>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();
/** The second line: what it is on right now, else the server's sub, else its tags. */
const line = computed(() => props.job.live || props.job.sub || (props.job.tags ?? []).map((t) => t.text).join(" · "));
</script>

<template>
  <button
    type="button"
    data-shape="code"
    :data-node="nodeId"
    :data-job="job.id"
    :data-status="job.status"
    :aria-pressed="selected ? 'true' : 'false'"
    :class="[
      'flex min-w-0 flex-none flex-col gap-0.5 rounded-[4px] border bg-[var(--code)] px-2 py-1.5 text-left font-mono outline-none focus-visible:ring-2 focus-visible:ring-ring',
      narrow ? 'w-[88px]' : 'w-24',
      BOX[job.status],
      selected ? 'ring-2 ring-ring' : '',
      job.status === 'running' ? 'motion-safe:animate-pulse' : '',
    ]"
    @click="emit('pick', nodeId, job.id)"
  >
    <span class="flex w-full min-w-0 items-center gap-[5px] text-xs font-semibold">
      <span data-square :class="['size-[7px] flex-none rounded-[1px]', DOT[job.status]]"></span>
      <span class="truncate">{{ job.label }}</span>
    </span>
    <span v-if="line" data-sub class="w-full truncate text-[11px] text-muted-foreground">{{ line }}</span>
  </button>
</template>
