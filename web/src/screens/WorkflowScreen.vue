<script setup lang="ts">
import { ChevronLeft, Square } from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import { ApiError } from "@/api";
import { cancelJob, listJobs, listRunning, storyFlow, storyJob } from "@/api/workflows";
import type { Flow } from "@/api/workflows";
import ErrorBanner from "@/components/ErrorBanner.vue";
import ItemKey from "@/components/ItemKey.vue";
import StatusChip from "@/components/StatusChip.vue";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/composables/useConfirm";
import { liveResource } from "@/stores/live";
import JobDetailView from "./workflow/JobDetail.vue";
import Pipeline from "./workflow/Pipeline.vue";

const route = useRoute();
const router = useRouter();
const { confirm } = useConfirm();

const key = computed(() => String(route.params.key));
const param = (v: unknown) => (typeof v === "string" && v ? v : undefined);

// undefined: still loading; null: there is no such story
const flow = liveResource<Flow | null>(["stories", "runs"], async () => {
  try {
    return await storyFlow(key.value);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
});
// another story in the same view (a link, Back): its own flow
watch(key, () => void flow.refresh());
const jobs = liveResource("jobs", listJobs);
const running = liveResource(["stories", "runs", "jobs"], listRunning);

/** The job open in the view: the one in the address, else the failed one, else the running one, else the last
 * that ran. */
const selected = computed<[string, string] | null>(() => {
  const nodes = flow.data.value?.nodes ?? [];
  const all = nodes.flatMap((n) => n.jobs.map((j) => [n.id, j.id, j.status] as const));
  const node = param(route.params.node);
  const job = param(route.params.job);
  if (node && job && all.some((x) => x[0] === node && x[1] === job)) return [node, job];
  const pick = all.find((x) => x[2] === "failed") ?? all.find((x) => x[2] === "running") ?? [...all].reverse().find((x) => x[2] === "passed");
  return pick ? [pick[0], pick[1]] : null;
});

const detail = liveResource(["stories", "runs"], async () => (selected.value ? storyJob(key.value, selected.value[0], selected.value[1]) : null));
watch(
  () => [key.value, ...(selected.value ?? [])].join("/"),
  () => void detail.refresh(),
);
// the detail on show is the one for the selection (the previous one stays until the new one lands)
const shown = computed(() => {
  const d = detail.data.value;
  return d && selected.value && d.node === selected.value[0] && d.job.id === selected.value[1] ? d : null;
});
const nodeLabel = computed(() => flow.data.value?.nodes.find((n) => n.id === selected.value?.[0])?.label);

function pick(node: string, job: string) {
  void router.push({ path: `/workflows/${encodeURIComponent(key.value)}/${encodeURIComponent(node)}/${encodeURIComponent(job)}` });
}

const progress = computed(() => {
  const row = running.data.value?.find((r) => r.kind === "story" && r.key === key.value);
  return row?.progress == null ? null : row.progress * 100;
});

// Stop is only offered where this server runs the story's job (jobs do not survive a restart)
const live = computed(() => jobs.data.value?.find((j) => j.id === `story-${key.value}` && j.state === "running"));
const stopError = ref<string>();
async function stop() {
  const job = live.value;
  if (!job || !(await confirm(`Stop ${key.value}? The story stops where it is; nothing already built is lost.`))) return;
  stopError.value = undefined;
  try {
    await cancelJob(job.id);
  } catch (e) {
    stopError.value = e instanceof Error ? e.message : String(e);
  }
  await jobs.refresh();
}
</script>

<template>
  <div class="flex flex-col gap-[18px] px-5 pt-6 pb-10 max-[640px]:gap-3.5 max-[640px]:px-3 max-[640px]:pt-4">
    <template v-if="flow.data.value === null">
      <RouterLink to="/workflows" class="inline-flex items-center gap-1 text-[13px] text-muted-foreground"><ChevronLeft class="size-4" />Workflows</RouterLink>
      <ErrorBanner :error="`No story ${key}.`" />
    </template>
    <ErrorBanner v-else-if="flow.error.value && !flow.data.value" :error="flow.error.value" />

    <template v-else-if="flow.data.value">
      <section class="flex items-start gap-4 rounded-xl border border-border bg-card p-[18px] shadow-sm max-[640px]:flex-col">
        <div class="flex min-w-0 flex-1 flex-col gap-2">
          <RouterLink to="/workflows" class="inline-flex items-center gap-1 self-start text-[13px] text-muted-foreground"><ChevronLeft class="size-4" />Workflows</RouterLink>
          <div class="flex flex-wrap items-center gap-2">
            <ItemKey :value="flow.data.value.key" />
            <StatusChip kind="stage" :value="flow.data.value.blocked ? 'blocked' : (flow.data.value.stage ?? 'new')" />
            <RouterLink :to="`/board/${encodeURIComponent(flow.data.value.key)}`" class="text-[13px] font-medium text-primary">Board card</RouterLink>
          </div>
          <h1 class="break-words text-[22px] font-bold tracking-[-0.01em] max-[640px]:text-xl">{{ flow.data.value.title ?? flow.data.value.key }}</h1>
          <p v-if="flow.data.value.blocked" role="alert" class="break-words rounded-lg bg-danger px-3 py-2 text-xs font-medium text-danger-foreground">{{ flow.data.value.blocked }}</p>
        </div>
        <Button v-if="live" type="button" data-stop variant="destructive" size="sm" class="font-semibold" @click="stop"><Square class="size-4" />Stop</Button>
      </section>
      <ErrorBanner v-if="stopError" :error="stopError" />

      <Pipeline :nodes="flow.data.value.nodes" :selected="selected" :progress="progress" @pick="pick" />

      <ErrorBanner v-if="detail.error.value" :error="detail.error.value" />
      <JobDetailView v-if="shown" :detail="shown" :node-label="nodeLabel" />
      <section v-else-if="!selected" aria-live="polite" class="rounded-xl border border-border bg-card p-[18px] text-muted-foreground shadow-sm">Nothing has run yet.</section>
    </template>
  </div>
</template>
