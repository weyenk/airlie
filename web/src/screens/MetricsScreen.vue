<script setup lang="ts">
import { ChevronDown } from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { metricsSeries, metricsSummary } from "@/api/metrics";
import type { MetricSlice } from "@/api/metrics";
import { listStories } from "@/api/workflows";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { liveResource } from "@/stores/live";
import { bucketSeconds, DEFAULT_RANGE, previousWindow, RANGES, windowOf } from "./metrics/math";
import SeriesChart from "./metrics/SeriesChart.vue";
import StatTiles from "./metrics/StatTiles.vue";

const SLICES: MetricSlice[] = ["role", "model", "topology", "task", "agent"];
const BUCKET_WORDS: Record<number, string> = { 300: "5-minute", 3600: "hourly", 21600: "6-hour", 86400: "daily" };

const router = useRouter();
const range = ref(DEFAULT_RANGE);
const slice = ref<MetricSlice>("role");

// The summary for the window and for the period before it, and the series: one load, so a screen never mixes two ranges.
// Runs and jobs are what produce metrics; the sums are recomputed from the files on every call.
const metrics = liveResource(["runs", "jobs", "stories"], async () => {
  const r = range.value;
  const s = slice.value;
  const window = windowOf(r, Math.floor(Date.now() / 1000));
  const before = previousWindow(r, window);
  const [summary, previous, series, active] = await Promise.all([
    metricsSummary({ ...window, slice: s }),
    before ? metricsSummary({ ...before, slice: s }) : Promise.resolve(null),
    metricsSeries({ metric: "generation_tps", ...window, slice: s }),
    // the Stories tile counts these and breaks the same ones down; the list's window is [from, to), so one past now
    listStories({ active_from: window.from, active_to: window.to + 1 }),
  ]);
  return { stories: active.stories, summary, previous, series, window, bucket: bucketSeconds(window.to - window.from), range: r, slice: s };
});
watch([range, slice], () => void metrics.refresh());

const shown = computed(() => metrics.data.value);
const loaded = computed(() => (shown.value ? shown.value.slice : slice.value));
function open(query: { active_from: string; active_to: string }) {
  void router.push({ path: "/workflows", query });
}
const selectClass =
  "h-[34px] appearance-none rounded-md border border-input bg-background pl-2.5 pr-7 text-[13px] text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring";
</script>

<template>
  <div class="mx-auto flex max-w-[1160px] flex-col gap-[18px] px-5 pt-6 pb-10 max-[640px]:gap-3.5 max-[640px]:px-3 max-[640px]:pt-4">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 class="text-[22px] font-bold tracking-[-0.01em] max-[640px]:text-xl">Metrics</h1>
        <div class="mt-1 text-muted-foreground">Where to look when something feels off.</div>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Time range" class="inline-flex gap-0.5 rounded-lg bg-muted p-[3px]">
          <button
            v-for="r in RANGES"
            :key="r.value"
            type="button"
            :data-range="r.value"
            :aria-pressed="range.value === r.value"
            :class="['whitespace-nowrap rounded-md px-[11px] py-[5px] text-[13px] font-medium', range.value === r.value ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground']"
            @click="range = r"
          >
            {{ r.label }}
          </button>
        </div>
        <label class="flex items-center gap-2 whitespace-nowrap text-[13px] text-muted-foreground">
          Slice by
          <span class="relative inline-flex items-center">
            <select v-model="slice" data-slice :class="selectClass">
              <option v-for="s in SLICES" :key="s" :value="s">{{ s }}</option>
            </select>
            <ChevronDown class="pointer-events-none absolute right-2 size-4" />
          </span>
        </label>
      </div>
    </div>

    <ErrorBanner v-if="metrics.error.value" :error="metrics.error.value" />

    <StatTiles :summary="shown?.summary" :previous="shown?.previous" :range="shown?.range ?? range" :stories="shown?.stories" />

    <section aria-label="Generation speed" class="rounded-xl border border-border bg-card shadow-sm">
      <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
        <h2 class="text-[15px] font-semibold">Generation speed by {{ loaded }}</h2>
        <span class="grow" />
        <span v-if="shown" class="text-xs text-muted-foreground">{{ BUCKET_WORDS[shown.bucket] }} median, tok/s</span>
      </div>
      <div v-if="!shown && !metrics.error.value" class="px-4 py-8 text-center text-sm text-muted-foreground">Loading…</div>
      <SeriesChart
        v-else-if="shown"
        :rows="shown.series"
        :slice="shown.slice"
        :window="shown.window"
        :bucket="shown.bucket"
        :title="`Generation speed by ${shown.slice}`"
        @open="open"
      />
    </section>

    <section class="rounded-xl border border-dashed border-border px-[18px] py-3.5 text-[13px] text-muted-foreground">
      Planned in the Metrics tab's own design and not built yet: "Looks off", the stories table and the trace. They drop in here, in this look, when that work is done.
    </section>
  </div>
</template>
