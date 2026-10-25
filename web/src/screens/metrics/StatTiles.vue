<script setup lang="ts">
import { computed } from "vue";
import type { MetricsSummary } from "@/api/metrics";
import type { StoryRow } from "@/api/workflows";
import { chipSpec } from "@/components/statusTable";
import { compare, formatRate } from "./math";
import type { Change, Range } from "./math";

const props = defineProps<{
  summary: MetricsSummary | undefined;
  /** The period just before: null when the range has none (All time), undefined while it loads. */
  previous: MetricsSummary | null | undefined;
  range: Range;
  /** Every story, for the Stories tile's breakdown. */
  stories: StoryRow[] | undefined;
}>();

type Tile = { name: string; label: string; value: number | null | undefined; unit: string; change: Change | null; goodWhenUp: boolean };

const tiles = computed<Tile[]>(() => {
  const s = props.summary;
  const p = props.previous;
  const against = (pick: (m: MetricsSummary) => number | null): Change | null => (s && p ? compare(pick(s), pick(p)) : null);
  return [
    { name: "generation", label: "Generation tok/s", value: s?.generation_tps, unit: "tok/s", change: against((m) => m.generation_tps), goodWhenUp: true },
    { name: "prefill", label: "Prefill tok/s", value: s?.prefill_tps, unit: "tok/s", change: against((m) => m.prefill_tps), goodWhenUp: true },
    { name: "stories", label: "Stories", value: s?.stories, unit: "", change: null, goodWhenUp: true },
    { name: "fix-rounds", label: "Fix rounds per layer", value: s?.fix_rounds_per_layer, unit: "", change: against((m) => m.fix_rounds_per_layer), goodWhenUp: false },
  ];
});

/** "2 in review · 1 building · 1 blocked": the stories by the state they are in, blocked ones apart. */
const breakdown = computed(() => {
  if (!props.stories) return "";
  const counts = new Map<string, number>();
  for (const st of props.stories) {
    const word = st.blocked ? "blocked" : chipSpec("stage", st.stage ?? "new").label;
    counts.set(word, (counts.get(word) ?? 0) + 1);
  }
  const blocked = counts.get("blocked");
  counts.delete("blocked");
  return [...counts, ...(blocked ? [["blocked", blocked] as const] : [])].map(([word, n]) => `${n} ${word}`).join(" · ");
});

function words(t: Tile): string {
  if (t.name === "stories") return breakdown.value;
  if (props.previous === null) return "all time";
  const c = t.change;
  if (!c) return "";
  const before = props.range.before;
  if (c.kind === "none") return `no data for the ${before}`;
  if (c.kind === "flat") return `same as the ${before}`;
  const arrow = c.kind === "up" ? "▲" : "▼";
  return c.percent === null ? `${arrow} from 0 in the ${before}` : `${arrow} ${c.percent}% vs the ${before}`;
}

function tone(t: Tile): string {
  const k = t.change?.kind;
  if (k !== "up" && k !== "down") return "text-muted-foreground";
  return (k === "up") === t.goodWhenUp ? "text-ok-foreground" : "text-danger-foreground";
}
</script>

<template>
  <div class="grid grid-cols-4 gap-3 max-[640px]:grid-cols-2">
    <section v-for="t in tiles" :key="t.name" :data-tile="t.name" class="rounded-xl border border-border bg-card p-4 shadow-sm">
      <div class="text-[13px] text-muted-foreground">{{ t.label }}</div>
      <div data-value class="mt-1.5 text-[28px] font-bold tabular-nums tracking-[-0.01em] max-[640px]:text-[22px]">{{ summary ? formatRate(t.value) : "…" }}<small v-if="summary && t.unit && t.value !== null && t.value !== undefined && Number.isFinite(t.value)" class="ml-1 text-sm font-medium text-muted-foreground">{{ t.unit }}</small></div>
      <div data-delta :class="['mt-1 min-h-4 text-xs font-semibold', tone(t)]">{{ words(t) }}</div>
    </section>
  </div>
</template>
