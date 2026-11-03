<script setup lang="ts">
import type { FlowJob } from "@/api/workflows";
import CodeBox from "./CodeBox.vue";
import JobBox from "./JobBox.vue";
import JobHover from "./JobHover.vue";
import MilestoneMark from "./MilestoneMark.vue";

/** One step's box: its shape picks the look, its card opens on hover (on a laptop). */
const props = defineProps<{ nodeId: string; job: FlowJob; selected?: boolean; narrow?: boolean }>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();

const clock = (at: number | null) => (at ? new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : null);
const pick = () => emit("pick", props.nodeId, props.job.id);
</script>

<template>
  <JobHover :card="job.card">
    <button
      v-if="job.shape === 'milestone'"
      type="button"
      data-shape="milestone"
      :data-node="nodeId"
      :data-job="job.id"
      :data-status="job.status"
      :aria-pressed="selected ? 'true' : 'false'"
      :class="['flex-none rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring', selected ? 'ring-2 ring-ring' : '']"
      @click="pick"
    >
      <MilestoneMark :label="job.label.toUpperCase()" :status="job.status" :at="clock(job.started)" />
    </button>
    <CodeBox v-else-if="job.shape === 'code'" :node-id="nodeId" :job="job" :selected="selected" :narrow="narrow" @pick="(n, j) => emit('pick', n, j)" />
    <JobBox v-else :node-id="nodeId" :job="job" :selected="selected" :narrow="narrow" @pick="(n, j) => emit('pick', n, j)" />
  </JobHover>
</template>
