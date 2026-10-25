<script setup lang="ts">
import { useElementSize, useMediaQuery } from "@vueuse/core";
import { computed, ref } from "vue";
import type { SeriesRow } from "@/api/metrics";
import EmptyState from "@/components/EmptyState.vue";
import { activityWindow, formatRate, niceTicks } from "./math";
import type { Window } from "./math";

/** The four series colours, in order; the only colour literals in the app besides the work-type colours. */
const SERIES_COLOURS = ["#34d399", "#38bdf8", "#fb923c", "#a78bfa"];

const props = defineProps<{ rows: SeriesRow[]; slice: string; window: Window; bucket: number; title: string }>();
const emit = defineEmits<{ open: [window: { active_from: string; active_to: string }] }>();

const LEFT = 40;
const RIGHT = 8;
const TOP = 12;
const BOTTOM = 24;

const plot = ref<HTMLElement>();
const size = useElementSize(plot);
const phone = useMediaQuery("(max-width: 640px)");
const width = computed(() => size.width.value || 800);
const height = computed(() => (phone.value ? 220 : 300));
const inner = computed(() => Math.max(1, width.value - LEFT - RIGHT));

const name = (r: SeriesRow): string => {
  const v = r[props.slice];
  return v === undefined || v === null || v === "" ? "unknown" : String(v);
};
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

const slices = computed(() => [...new Set(props.rows.map(name))].sort());
const colour = (s: string) => SERIES_COLOURS[slices.value.indexOf(s) % SERIES_COLOURS.length];
const times = computed(() => [...new Set(props.rows.filter((r) => finite(r.t)).map((r) => r.t))].sort((a, b) => a - b));

// All time starts at the epoch: the line starts at the first bucket instead
const start = computed(() => (props.window.from <= 0 ? (times.value[0] ?? 0) : props.window.from));
const end = computed(() => Math.max(props.window.to, start.value + 1));
const x = (t: number) => LEFT + ((t - start.value) / (end.value - start.value)) * inner.value;

const ticks = computed(() => niceTicks(Math.max(0, ...props.rows.map((r) => (finite(r.p50) ? r.p50 : 0)))));
const top = computed(() => ticks.value[ticks.value.length - 1]);
const y = (v: number) => TOP + (height.value - TOP - BOTTOM) * (1 - v / top.value);

const lines = computed(() =>
  slices.value.map((s) => {
    const points = props.rows
      .filter((r) => name(r) === s && finite(r.t) && finite(r.p50))
      .sort((a, b) => a.t - b.t)
      .map((r) => [x(r.t), y(r.p50)] as const);
    return { name: s, colour: colour(s), points, d: points.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)} ${py.toFixed(1)}`).join(" ") };
  }),
);

function stamp(t: number, long: boolean): string {
  const d = new Date(t * 1000);
  const day = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  return props.bucket >= 86400 && !long ? day : props.bucket >= 86400 ? day : `${day}, ${d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false })}`;
}
const xLabels = computed(() => {
  const n = Math.max(2, Math.min(7, Math.floor(inner.value / 90)));
  const span = end.value - start.value;
  return Array.from({ length: n }, (_, i) => {
    const t = start.value + (span * i) / (n - 1);
    const d = new Date(t * 1000);
    const text = span <= 24 * 3600 ? d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false }) : stamp(t, false).split(",")[0];
    return { text, px: Math.min(x(t), width.value - 18), anchor: i === 0 ? "start" : i === n - 1 ? "end" : "middle" };
  });
});

const active = ref<number | null>(null);
const tipRows = computed(() =>
  active.value === null
    ? []
    : slices.value.map((s) => {
        const hit = props.rows.find((r) => name(r) === s && r.t === active.value && finite(r.p50));
        return { name: s, colour: colour(s), value: hit ? formatRate(hit.p50) : "unknown" };
      }),
);
const tipStyle = computed(() => {
  if (active.value === null) return {};
  const px = x(active.value);
  return px > width.value / 2 ? { right: `${width.value - px + 12}px`, top: "24px" } : { left: `${px + 12}px`, top: "24px" };
});

function nearest(clientX: number): number | null {
  if (!times.value.length) return null;
  const px = clientX - (plot.value?.getBoundingClientRect().left ?? 0);
  return times.value.reduce((best, t) => (Math.abs(x(t) - px) < Math.abs(x(best) - px) ? t : best));
}
const pointer = (e: MouseEvent) => (active.value = nearest(e.clientX));
function open() {
  if (active.value !== null) emit("open", activityWindow(active.value, props.bucket));
}
function click(e: MouseEvent) {
  pointer(e);
  open();
}
function key(e: KeyboardEvent) {
  const ts = times.value;
  if (!ts.length) return;
  const i = active.value === null ? -1 : ts.indexOf(active.value);
  if (e.key === "ArrowRight") active.value = ts[Math.min(ts.length - 1, i + 1)];
  else if (e.key === "ArrowLeft") active.value = ts[Math.max(0, i < 0 ? 0 : i - 1)];
  else if (e.key === "Home") active.value = ts[0];
  else if (e.key === "End") active.value = ts[ts.length - 1];
  else if (e.key === "Escape") active.value = null;
  else if (e.key === "Enter" || e.key === " ") open();
  else return;
  e.preventDefault();
}
</script>

<template>
  <div>
    <EmptyState v-if="!rows.length" text="No runs in this range" />
    <template v-else>
      <div data-legend class="flex flex-wrap gap-3.5 px-[18px] pt-3 text-xs text-muted-foreground">
        <span v-for="l in lines" :key="l.name"><i class="mr-1.5 inline-block h-[3px] w-2.5 rounded-sm align-middle" :style="{ background: l.colour }" />{{ l.name }}</span>
      </div>
      <div class="relative px-[18px] pt-2 pb-4 max-[640px]:px-2">
        <div
          ref="plot"
          data-plot
          tabindex="0"
          role="group"
          :aria-label="`${title}. Left and right arrows move between points; Enter opens the stories active then.`"
          class="relative cursor-pointer rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @mousemove="pointer"
          @mouseleave="active = null"
          @blur="active = null"
          @click="click"
          @keydown="key"
        >
          <svg :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`" aria-hidden="true" class="block w-full overflow-visible" fill="none" stroke-width="2">
            <g class="stroke-border" stroke-width="1">
              <line v-for="t in ticks" :key="t" :x1="LEFT" :x2="width - RIGHT" :y1="y(t)" :y2="y(t)" />
            </g>
            <text v-for="t in ticks" :key="`y${t}`" x="0" :y="y(t) + 4" class="fill-muted-foreground" stroke="none" font-size="11">{{ formatRate(t) }}</text>
            <text v-for="l in xLabels" :key="l.text + l.px" :x="l.px" :y="height - 6" :text-anchor="l.anchor" class="fill-muted-foreground" stroke="none" font-size="11">{{ l.text }}</text>
            <g v-for="l in lines" :key="l.name">
              <path v-if="l.d" :data-line="l.name" :d="l.d" :stroke="l.colour" fill="none" stroke-linejoin="round" />
              <circle v-for="([px, py], i) in l.points" :key="i" :cx="px" :cy="py" r="2.5" :fill="l.colour" stroke="none" />
            </g>
            <template v-if="active !== null">
              <line :x1="x(active)" :x2="x(active)" :y1="TOP" :y2="height - BOTTOM" class="stroke-muted-foreground" stroke-width="1" stroke-dasharray="3 4" />
              <template v-for="l in lines" :key="l.name">
                <circle v-for="r in rows.filter((r) => name(r) === l.name && r.t === active && finite(r.p50))" :key="r.t" :cx="x(r.t)" :cy="y(r.p50)" r="5" :fill="l.colour" class="stroke-card" stroke-width="2" />
              </template>
            </template>
          </svg>
          <div v-if="active !== null" data-tip role="status" class="pointer-events-none absolute rounded-lg border border-border bg-card px-2.5 py-2 text-xs shadow-lg max-[640px]:hidden" :style="tipStyle">
            <strong>{{ stamp(active, true) }}</strong>
            <div v-for="r in tipRows" :key="r.name" class="flex justify-between gap-2">
              <span><i class="mr-1.5 inline-block size-2 rounded-full align-middle" :style="{ background: r.colour }" />{{ r.name }}</span>
              <span class="font-mono">{{ r.value }}</span>
            </div>
            <div class="mt-1 text-muted-foreground">Click for the stories active then</div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
