<script setup lang="ts">
import type { FlowNode } from "@/api/workflows";
import ItemKey from "@/components/ItemKey.vue";
import JobBox from "./JobBox.vue";
import { DOT } from "./style";

const props = defineProps<{ node: FlowNode; selected: [string, string] | null }>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();

const text = () => `${props.node.label}${props.node.title ? ` · ${props.node.title}` : ""}`;
</script>

<template>
  <div
    data-lane
    :data-node="node.id"
    :data-status="node.status"
    :class="['w-fit max-w-full flex-none rounded-xl border bg-background px-2.5 pt-2 pb-2.5', node.status === 'running' ? 'border-info' : 'border-border']"
  >
    <div class="mb-[7px] flex min-w-0 items-center gap-2 text-xs">
      <span :class="['size-2 flex-none rounded-full', DOT[node.status]]" />
      <strong data-title :title="text()" :class="['min-w-0 max-w-[420px] truncate font-semibold', node.status === 'pending' ? 'text-muted-foreground' : '']">{{ text() }}</strong>
      <span v-if="node.attempts && node.attempts > 1" class="shrink-0 whitespace-nowrap rounded-md bg-warn px-2 py-0.5 text-xs font-bold text-warn-foreground">{{ node.attempts }} attempts</span>
      <ItemKey v-if="node.acceptance?.length" data-acceptance :value="node.acceptance.join(' ')" />
    </div>
    <div class="flex items-center">
      <template v-for="(j, i) in node.jobs" :key="j.id">
        <span v-if="i" class="px-[5px] text-muted-foreground" aria-hidden="true">→</span>
        <JobBox :node-id="node.id" :job="j" narrow :selected="selected?.[0] === node.id && selected?.[1] === j.id" @pick="(n, jb) => emit('pick', n, jb)" />
      </template>
    </div>
  </div>
</template>
