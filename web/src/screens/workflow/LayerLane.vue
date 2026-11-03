<script setup lang="ts">
import { computed } from "vue";
import type { FlowNode, LaneRow } from "@/api/workflows";
import ItemKey from "@/components/ItemKey.vue";
import FlowBox from "./FlowBox.vue";
import JobHover from "./JobHover.vue";
import { DOT, TAG } from "./style";

const props = defineProps<{ node: FlowNode; selected: [string, string] | null }>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();

const text = () => `${props.node.label}${props.node.title ? ` · ${props.node.title}` : ""}`;

// an older flow without rows is one try cycle of its jobs
const rows = computed<LaneRow[]>(() => props.node.rows ?? [{ kind: "cycle", id: "c1", label: "build 1", status: props.node.status, jobs: props.node.jobs }]);
const started = computed(() => rows.value.some((r) => r.kind === "cycle" && r.jobs.some((j) => j.status !== "pending")));
const several = computed(() => rows.value.filter((r) => r.kind === "cycle").length > 1);
</script>

<template>
  <div
    data-lane
    :data-node="node.id"
    :data-status="node.status"
    :class="['w-fit max-w-full flex-none rounded-xl border bg-background px-2.5 pt-2 pb-2.5', node.status === 'running' ? 'border-info' : 'border-border']"
  >
    <div :class="['flex min-w-0 items-center gap-2 text-xs', started ? 'mb-[7px]' : '']">
      <span :class="['size-2 flex-none rounded-full', DOT[node.status]]" />
      <strong data-title :title="text()" :class="['min-w-0 max-w-[420px] truncate font-semibold', node.status === 'pending' ? 'text-muted-foreground' : '']">{{ text() }}</strong>
      <span v-if="node.attempts && node.attempts > 1" class="shrink-0 whitespace-nowrap rounded-md bg-warn px-2 py-0.5 text-xs font-bold text-warn-foreground">{{ node.attempts }} attempts</span>
      <span v-for="(t, i) in node.tags ?? []" :key="i" data-tag :class="['shrink-0 whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-bold', TAG[t.tone]]">{{ t.text }}</span>
      <ItemKey v-if="node.acceptance?.length" data-acceptance :value="node.acceptance.join(' ')" />
    </div>
    <template v-if="started">
      <template v-for="(r, k) in rows" :key="k">
        <div v-if="r.kind === 'cycle'" data-row :data-row-id="r.id" :data-status="r.status" class="flex items-center">
          <span v-if="several" class="mr-2 w-14 flex-none text-xs text-muted-foreground">{{ r.label }}</span>
          <template v-for="(j, i) in r.jobs" :key="j.id">
            <span v-if="i" class="px-[5px] text-muted-foreground" aria-hidden="true">→</span>
            <FlowBox :node-id="node.id" :job="j" narrow :selected="selected?.[0] === node.id && selected?.[1] === j.id" @pick="(n, jb) => emit('pick', n, jb)" />
          </template>
        </div>
        <JobHover v-else :card="r.card">
          <div data-divider tabindex="0" class="my-1.5 flex items-center gap-2 text-xs text-muted-foreground">
            <span class="h-px grow bg-border"></span>
            <span class="flex-none">{{ r.text }}</span>
            <span class="h-px grow bg-border"></span>
          </div>
        </JobHover>
      </template>
    </template>
  </div>
</template>
