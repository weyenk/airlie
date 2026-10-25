<script setup lang="ts">
import { computed } from "vue";
import type { JobDetail } from "@/api/workflows";
import StatusChip from "@/components/StatusChip.vue";
import { stats } from "./format";
import StepList from "./StepList.vue";

// Any job's detail, a story's or a run's: it assumes nothing about where the job came from.
const props = defineProps<{ detail: JobDetail; nodeLabel?: string }>();

const job = computed(() => props.detail.job);
const heading = computed(() => (props.nodeLabel ? `${props.nodeLabel} / ${job.value.label}` : job.value.label));

const LABELS: Record<string, string> = { story: "story", layers: "layers", layer: "layer brief", built: "built", survey: "survey notes", final_text: "final reply" };
/** What the job produced, as [label, text] pairs: only the parts it has. */
const output = computed(() =>
  Object.entries(props.detail.output)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]): [string, string] => [LABELS[k] ?? k, typeof v === "string" ? v : JSON.stringify(v, null, 2)]),
);
const PRE = "whitespace-pre-wrap rounded-lg border border-border bg-code px-3 py-2.5 font-mono text-xs leading-normal";
</script>

<template>
  <section aria-label="Job" class="rounded-xl border border-border bg-card p-[18px] shadow-sm">
    <p aria-live="polite" class="sr-only">{{ heading }}: {{ job.status }}</p>
    <div class="flex flex-wrap items-center gap-2">
      <h2 class="text-[15px] font-semibold">{{ heading }}</h2>
      <StatusChip kind="job" :value="job.status" />
      <span v-if="job.role" class="rounded-md bg-chip px-2 py-0.5 text-xs font-bold text-chip-foreground">{{ job.role }}</span>
      <span class="grow" />
      <span v-if="job.studio_run" class="font-mono text-xs text-muted-foreground">run {{ job.studio_run }}</span>
    </div>

    <div class="mt-3.5 mb-[18px] grid grid-cols-[repeat(auto-fill,minmax(118px,1fr))] gap-2 max-[640px]:grid-cols-2">
      <div v-for="[k, v] in stats(job)" :key="k" class="min-w-0 rounded-[10px] border border-border px-[11px] py-[9px]">
        <div class="break-words font-mono text-sm font-semibold">{{ v }}</div>
        <div class="mt-0.5 text-xs text-muted-foreground">{{ k }}</div>
      </div>
    </div>

    <div v-if="job.retries.length" class="mb-[18px]">
      <h3 class="mb-2 text-[13px] font-semibold text-muted-foreground">Why it retried</h3>
      <div v-for="(r, i) in job.retries" :key="i" class="flex gap-2.5 py-0.5 text-[13px]">
        <code class="flex-none font-mono text-warn-foreground">{{ r.kind }}</code>
        <span class="min-w-0 break-words">{{ r.detail }}</span>
      </div>
    </div>

    <div class="mb-[18px] grid grid-cols-2 gap-4 max-[640px]:grid-cols-1">
      <div class="min-w-0">
        <h3 class="mb-2 text-[13px] font-semibold text-muted-foreground">Input: what it was given</h3>
        <template v-if="detail.input">
          <div v-for="(m, i) in detail.input.messages" :key="i">
            <div class="mt-2 mb-1 font-mono text-xs text-muted-foreground">{{ m.role }}</div>
            <pre :class="PRE">{{ m.content }}</pre>
          </div>
        </template>
        <p v-else class="text-xs text-muted-foreground">{{ detail.input_note }}</p>
      </div>
      <div class="min-w-0">
        <h3 class="mb-2 text-[13px] font-semibold text-muted-foreground">Output: what it produced</h3>
        <template v-if="output.length">
          <div v-for="[label, text] in output" :key="label">
            <div class="mt-2 mb-1 font-mono text-xs text-muted-foreground">{{ label }}</div>
            <pre :class="PRE">{{ text }}</pre>
          </div>
        </template>
        <p v-else-if="job.status === 'running'" class="text-xs text-muted-foreground">Still running. The output appears when the job ends.</p>
        <p v-else class="text-xs text-muted-foreground">Nothing recorded.</p>
      </div>
    </div>

    <template v-if="detail.steps.length">
      <h3 class="mb-2 text-[13px] font-semibold text-muted-foreground">Steps ({{ detail.steps.length }})</h3>
      <StepList :steps="detail.steps" />
    </template>
  </section>
</template>
