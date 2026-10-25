<script setup lang="ts">
import { ChevronLeft, Square } from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import { ApiError } from "@/api";
import { cancelJob, runFlow, runJob, startRun } from "@/api/workflows";
import type { Flow } from "@/api/workflows";
import ErrorBanner from "@/components/ErrorBanner.vue";
import ProgressBar from "@/components/ProgressBar.vue";
import StatusChip from "@/components/StatusChip.vue";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/composables/useConfirm";
import { liveResource } from "@/stores/live";
import JobDetailView from "./workflow/JobDetail.vue";
import PlanCard from "./workflow/PlanCard.vue";
import { useJobSelection } from "./workflow/selection";
import StageBox from "./workflow/StageBox.vue";

const route = useRoute();
const router = useRouter();
const { confirm } = useConfirm();

const id = computed(() => String(route.params.id));

// undefined: still loading; null: there is no such run
const flow = liveResource<Flow | null>(["runs", "jobs"], async () => {
  try {
    return await runFlow(id.value);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
});
// another run in the same view (a link, Back, a new run after Approve): its own flow
watch(id, () => void flow.refresh());
/** The flow of this run (not a previous run's, while another loads). */
const current = computed(() => (flow.data.value && flow.data.value.key === id.value ? flow.data.value : undefined));

const { selected } = useJobSelection(id, flow.data);

const detail = liveResource(["runs", "jobs"], async () => (selected.value ? runJob(id.value, selected.value[0], selected.value[1]) : null));
watch(
  () => [id.value, ...(selected.value ?? [])].join("/"),
  () => void detail.refresh(),
);
// only the detail of the selected job is shown; a loading line stands in until the new one lands
const shown = computed(() => {
  const d = detail.data.value;
  return d && selected.value && d.node === selected.value[0] && d.job.id === selected.value[1] ? d : null;
});
const nodeLabel = computed(() => flow.data.value?.nodes.find((n) => n.id === selected.value?.[0])?.label);

function pick(node: string, job: string) {
  void router.push({ path: `/workflows/runs/${encodeURIComponent(id.value)}/${encodeURIComponent(node)}/${encodeURIComponent(job)}` });
}

const stages = computed(() => (current.value?.nodes ?? []).filter((n) => n.jobs.length));
const over = computed(() => current.value?.stage === "done");
/** The run's state as one chip: its plan awaiting a person, stopped by one, failed, done, or still going. */
const state = computed(() => {
  const f = current.value;
  if (!f) return "running";
  if (f.plan && f.job === "done") return "approval";
  if (f.job === "cancelled") return "stopped";
  if (f.stage !== "done") return "running";
  return f.ok === false || f.job === "failed" ? "failed" : "done";
});
// The figure only means something while the run goes: a finished fork run's tree can leave it below 1.
const progress = computed(() => (current.value && !over.value ? (current.value.progress ?? null) : undefined));
const figure = computed(() => (typeof progress.value === "number" ? `${Math.round(progress.value * 100)}%` : null));

const failure = ref<string>();
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

// Stop is only offered where this server runs the run's job (jobs do not survive a restart)
async function stop() {
  if (!(await confirm(`Stop this run? It stops where it is; nothing already built is lost.`))) return;
  failure.value = undefined;
  try {
    await cancelJob(id.value);
  } catch (e) {
    failure.value = message(e);
  }
  await flow.refresh();
}

const approving = ref(false);
async function approve(plan: Record<string, unknown>) {
  const f = current.value;
  if (!f?.repo || !f.title) {
    failure.value = "This run does not say which repository or goal it was for, so its plan cannot be approved here.";
    return;
  }
  failure.value = undefined;
  approving.value = true;
  try {
    const started = await startRun({ goal: f.title, repo: f.repo, plan });
    await router.push({ path: `/workflows/runs/${encodeURIComponent(started.job_id)}` });
  } catch (e) {
    failure.value = message(e);
  } finally {
    approving.value = false;
  }
}
</script>

<template>
  <div class="flex flex-col gap-[18px] px-5 pt-6 pb-10 max-[640px]:gap-3.5 max-[640px]:px-3 max-[640px]:pt-4">
    <template v-if="flow.data.value === null">
      <RouterLink to="/workflows" class="inline-flex items-center gap-1 text-[13px] text-muted-foreground"><ChevronLeft class="size-4" />Workflows</RouterLink>
      <ErrorBanner :error="`No run ${id}.`" />
    </template>
    <ErrorBanner v-else-if="flow.error.value && !current" :error="flow.error.value" />

    <template v-else-if="current">
      <section class="flex items-start gap-4 rounded-xl border border-border bg-card p-[18px] shadow-sm max-[640px]:flex-col">
        <div class="flex min-w-0 flex-1 flex-col gap-2">
          <RouterLink to="/workflows" class="inline-flex items-center gap-1 self-start text-[13px] text-muted-foreground"><ChevronLeft class="size-4" />Workflows</RouterLink>
          <div class="flex flex-wrap items-center gap-2">
            <StatusChip kind="run" :value="current.topology ?? 'single'" />
            <StatusChip kind="job" :value="state" />
            <span class="break-all font-mono text-xs text-muted-foreground">{{ current.key }}<template v-if="current.repo"> · {{ current.repo }}</template></span>
          </div>
          <h1 class="break-words text-[22px] font-bold tracking-[-0.01em] max-[640px]:text-xl">{{ current.title ?? current.key }}</h1>
        </div>
        <Button v-if="current.job === 'running'" type="button" data-stop variant="destructive" size="sm" class="font-semibold" @click="stop"><Square class="size-4" />Stop</Button>
      </section>
      <ErrorBanner v-if="failure" :error="failure" />

      <section aria-label="Pipeline" class="rounded-xl border border-border bg-card shadow-sm">
        <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
          <h2 class="text-[15px] font-semibold">Pipeline</h2>
          <span class="grow" />
          <span v-if="figure" data-progress class="text-xs text-muted-foreground">{{ figure }}</span>
        </div>
        <div v-if="progress !== undefined" class="px-[18px] pt-3.5"><ProgressBar :value="progress === null ? null : progress * 100" /></div>
        <div data-flow class="flex items-center overflow-x-auto p-[18px] max-[640px]:flex-col max-[640px]:items-stretch">
          <template v-for="(n, i) in stages" :key="n.id">
            <span v-if="i" class="flex-none px-2 text-muted-foreground max-[640px]:py-1 max-[640px]:text-center max-[640px]:before:content-['↓'] min-[641px]:before:content-['→']" aria-hidden="true" />
            <StageBox :node="n" :selected="selected" @pick="pick" />
          </template>
          <p v-if="!stages.length" class="text-muted-foreground">Nothing has run yet.</p>
        </div>
      </section>

      <PlanCard v-if="current.plan" :key="current.key" :plan="current.plan" :busy="approving" @approve="approve" />

      <ErrorBanner v-if="detail.error.value" :error="detail.error.value" />
      <p aria-live="polite" class="sr-only">{{ shown ? `${nodeLabel ? nodeLabel + " / " : ""}${shown.job.label}: ${shown.job.status}` : "" }}</p>
      <JobDetailView v-if="shown" :detail="shown" :node-label="nodeLabel" />
      <section v-else-if="selected && !detail.error.value" aria-label="Job" class="rounded-xl border border-border bg-card p-[18px] text-muted-foreground shadow-sm">Loading…</section>
    </template>
  </div>
</template>
