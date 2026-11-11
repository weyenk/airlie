<script setup lang="ts">
import { computed } from "vue";
import type { EngineStatus } from "@/api/engines";
import ProgressBar from "./ProgressBar.vue";

const props = defineProps<{ status: EngineStatus }>();

const gb = (bytes: number) => (bytes / 1e9).toFixed(1);

function left(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s left`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min left`;
  return `${(seconds / 3600).toFixed(1)} h left`;
}

/** The progress, 0 to 100, while the size is known. */
const percent = computed(() => {
  const p = props.status.progress;
  return p && p.total_bytes > 0 ? (p.done_bytes / p.total_bytes) * 100 : null;
});

/** What the engine is doing, said for a person; null when there is nothing to say (ready, or idle until asked). */
const text = computed<string | null>(() => {
  const { name, state, detail, progress: p } = props.status;
  switch (state) {
    case "downloading":
      if (!p || p.total_bytes <= 0) return `Downloading ${name}`;
      return `Downloading ${name}: ${gb(p.done_bytes)} of ${gb(p.total_bytes)} GB${p.eta_s === null ? "" : `, ${left(p.eta_s)}`}`;
    case "starting":
      return `Starting ${name}`;
    case "waiting":
      return detail ? detail.charAt(0).toUpperCase() + detail.slice(1) : "Waiting for memory";
    case "stopping":
      return `Stopping ${name}`;
    case "failed":
      return detail ? `${name} failed: ${detail}` : `${name} failed`;
    default:
      return null;
  }
});
</script>

<template>
  <div v-if="text" data-test="engine-status" class="flex min-w-[180px] flex-col gap-1 text-xs font-medium">
    <span role="status" :class="status.state === 'failed' ? 'text-danger-foreground' : 'text-muted-foreground'">{{ text }}</span>
    <ProgressBar v-if="status.state === 'downloading'" :value="percent" />
  </div>
</template>
