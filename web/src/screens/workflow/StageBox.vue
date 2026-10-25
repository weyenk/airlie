<script setup lang="ts">
import type { FlowNode } from "@/api/workflows";
import JobBox from "./JobBox.vue";
import { DOT } from "./style";

// A run's stage: a box holding its jobs. Parallel stages (teams, forks) stack their jobs, the winner starred.
defineProps<{ node: FlowNode; selected: [string, string] | null }>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();
</script>

<template>
  <div
    data-stage
    :data-node="node.id"
    :data-status="node.status"
    :class="['flex flex-none flex-col gap-[7px] rounded-xl border bg-background px-2.5 pt-2 pb-2.5 max-[640px]:w-full', node.status === 'running' ? 'border-info-foreground' : 'border-border']"
  >
    <div class="flex items-center gap-2 text-xs font-semibold">
      <span :class="['size-2 flex-none rounded-full', DOT[node.status]]" />
      <span :class="node.status === 'pending' ? 'text-muted-foreground' : ''">{{ node.label }}</span>
    </div>
    <JobBox
      v-for="j in node.jobs"
      :key="j.id"
      :node-id="node.id"
      :job="j"
      class="max-[640px]:!w-full"
      :selected="selected?.[0] === node.id && selected?.[1] === j.id"
      @pick="(n, jb) => emit('pick', n, jb)"
    />
  </div>
</template>
