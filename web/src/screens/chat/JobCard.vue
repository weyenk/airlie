<script setup lang="ts">
import { computed } from "vue";
import { listStories } from "@/api/workflows";
import type { StoryRow } from "@/api/workflows";
import ProgressBar from "@/components/ProgressBar.vue";
import { chipSpec } from "@/components/statusTable";
import { liveResource } from "@/stores/live";
import { useWorkflowsStore } from "@/stores/workflows";

/** A job a story or run started from the chat (`started`, or a `card` item written by today's app). */
const props = defineProps<{ job: { job_id?: string; kind?: string; label?: string } }>();

const id = computed(() => props.job.job_id ?? "");
const isStory = computed(() => id.value.startsWith("story-"));
const key = computed(() => (isStory.value ? id.value.slice(6) : id.value));
/** Where the card goes: a story's job is `story-<KEY>`, so its workflow view; a plan's or a run's job is its run. */
const href = computed(() => (isStory.value ? `/workflows/${encodeURIComponent(key.value)}` : `/workflows/runs/${encodeURIComponent(id.value)}`));
const kindText = computed(() => (props.job.kind === "plan" ? "planning" : props.job.kind === "story" || isStory.value ? "story started" : "job started"));

const workflows = useWorkflowsStore();
const row = computed(() => workflows.running.find((r) => r.key === key.value));
const progress = computed(() => (row.value ? (row.value.progress === null ? null : row.value.progress * 100) : undefined));

// a story's stage: its row, looked up again whenever stories change
const story = liveResource("stories", async (): Promise<StoryRow | undefined> => {
  if (!isStory.value) return undefined;
  return (await listStories({ q: key.value, size: 5 })).stories.find((s) => s.key === key.value);
});
const stage = computed(() => {
  if (!isStory.value) return "";
  const s = story.data.value;
  if (!s) return "starting";
  return chipSpec("stage", s.blocked ? "blocked" : (s.stage ?? "new")).label;
});
</script>

<template>
  <div data-card class="flex w-[560px] max-w-full min-w-0 items-center max-[640px]:flex-wrap gap-x-2.5 gap-y-1.5 rounded-xl border border-border bg-card px-3.5 py-[11px]">
    <span data-kind class="inline-flex flex-none items-center gap-[5px] rounded-md bg-info px-2 py-0.5 text-xs font-bold whitespace-nowrap text-info-foreground">
      <span class="size-[7px] rounded-full bg-current" />{{ kindText }}
    </span>
    <span class="min-w-0 max-w-full truncate font-mono text-[13px] text-muted-foreground" :title="job.label || id">{{ job.label || id }}</span>
    <span v-if="stage" data-stage class="text-[13px] whitespace-nowrap">{{ stage }}</span>
    <div v-if="progress !== undefined" class="w-[80px] flex-none max-[640px]:hidden"><ProgressBar :value="progress" /></div>
    <RouterLink :to="href" data-card-link class="ml-auto flex-none text-[13px] font-semibold whitespace-nowrap text-primary">Open workflow →</RouterLink>
  </div>
</template>
