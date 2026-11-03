<script setup lang="ts">
import { computed, ref } from "vue";
import type { CardRow, FlowExit, FlowJob, FlowNode, FlowNow, LaneRow } from "@/api/workflows";
import ExitRow from "./ExitRow.vue";
import { briefSpan } from "./format";
import NowBar from "./NowBar.vue";
import { DOT, MARK, TAG } from "./style";

/**
 * The pipeline on a phone: one list. A finished lane folds to a row of chips, a built or pending layer is one row,
 * and only the node holding the running (or last failed) job opens, with its inner steps under the running job.
 */
const props = defineProps<{ nodes: FlowNode[]; now?: FlowNow; exit?: FlowExit | null; selected: [string, string] | null }>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();

type Lane = "plan" | "build";
const LANES: { id: Lane; label: string; folded: string }[] = [
  { id: "plan", label: "PLAN LANE", folded: "Planned" },
  { id: "build", label: "BUILD LANE", folded: "Built" },
];
const laneOf = (n: FlowNode): Lane => n.lane ?? (n.kind === "layer" || n.id === "publish" || n.id === "review" ? "build" : "plan");
/** A stage with no jobs never ran and is not drawn; a layer always is. */
const drawn = computed(() => props.nodes.filter((n) => n.kind === "layer" || n.jobs.length));
const lanes = computed(() => LANES.map((l) => ({ ...l, nodes: drawn.value.filter((n) => laneOf(n) === l.id) })).filter((l) => l.nodes.length));

const unfolded = ref<Record<string, boolean>>({});
// done: every node passed or skipped, and nothing in it still running or failed (a stage can hold a second job)
const isDone = (nodes: FlowNode[]) => nodes.every((n) => (n.status === "passed" || n.status === "skipped") && !n.jobs.some((j) => j.status === "running" || j.status === "failed"));
const folded = (l: { id: string; nodes: FlowNode[] }) => isDone(l.nodes) && !unfolded.value[l.id];
const toggle = (id: string) => (unfolded.value = { ...unfolded.value, [id]: !unfolded.value[id] });

/** The rows of a layer; an older flow without rows is one try of its jobs. */
const rowsOf = (n: FlowNode): LaneRow[] => (n.kind === "layer" ? n.rows : undefined) ?? [{ kind: "cycle", id: "c1", label: "build 1", status: n.status, jobs: n.jobs }];
/** The jobs a node shows live: a layer's latest try, a stage's own. */
const liveJobs = (n: FlowNode): FlowJob[] => {
  const r = rowsOf(n)[lastCycleIndex(rowsOf(n))];
  return r?.kind === "cycle" ? r.jobs : [];
};
/** The node holding the running job, else the last with a failed one. */
const openId = computed(() => {
  const all = drawn.value;
  return (all.find((n) => liveJobs(n).some((j) => j.status === "running")) ?? [...all].reverse().find((n) => liveJobs(n).some((j) => j.status === "failed")))?.id ?? null;
});

const isSelected = (node: string, job: string) => props.selected?.[0] === node && props.selected?.[1] === job;
const pick = (node: string, job: string) => emit("pick", node, job);
const tail = (jobs: FlowJob[]) => jobs[jobs.length - 1];
const steps = (j: FlowJob): Extract<CardRow, { kind: "step" }>[] => (j.status === "running" ? (j.card?.rows ?? []).filter((r): r is Extract<CardRow, { kind: "step" }> => r.kind === "step") : []);
const sub = (j: FlowJob) => [j.role, j.duration_s ? briefSpan(j.duration_s) : null].filter(Boolean).join(" · ") || j.status;
const WORD: Record<string, string> = { failed: "red", passed: "green", running: "running", stopped: "stopped" };
/** "build 1 · red → re-planned": an earlier try, with the divider that follows it. */
const earlierText = (rows: LaneRow[], i: number) => {
  const r = rows[i];
  if (r.kind !== "cycle") return "";
  const next = rows[i + 1];
  const why = next?.kind === "divider" ? ` → ${next.text.replace(/^[^\p{L}\p{N}]+/u, "")}` : "";
  return `${r.label} · ${WORD[r.status] ?? r.status}${why}`;
};
const absorbed = (rows: LaneRow[], i: number) => rows[i].kind === "divider" && rows[i - 1]?.kind === "cycle" && i - 1 < lastCycleIndex(rows);
const lastCycleIndex = (rows: LaneRow[]) => {
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i].kind === "cycle") return i;
  return -1;
};
const tryCount = (n: FlowNode) => rowsOf(n).filter((r) => r.kind === "cycle").length;
const triesOpen = ref<Record<string, boolean>>({});
const toggleTries = (id: string) => (triesOpen.value = { ...triesOpen.value, [id]: !triesOpen.value[id] });
const rowCls = "flex w-full min-w-0 items-center gap-2.5 border-b border-border py-[9px] text-left outline-none focus-visible:ring-2 focus-visible:ring-ring";
</script>

<template>
  <div data-phone-list class="flex flex-col pb-3">
    <NowBar v-if="now" :now="now" class="sticky top-[env(safe-area-inset-top,0px)] z-10 bg-card" />

    <div class="flex flex-col px-3">
      <template v-for="lane in lanes" :key="lane.id">
        <div data-lane-label class="pt-3 pb-1 text-[11px] font-semibold tracking-[0.08em] text-muted-foreground">{{ lane.label }}</div>

        <template v-if="isDone(lane.nodes)">
          <button type="button" :data-folded="lane.id" :aria-expanded="!folded(lane)" :class="[rowCls, 'flex-wrap']" @click="toggle(lane.id)">
            <span :class="['size-2 flex-none rounded-full', DOT.passed]" />
            <span class="min-w-0 truncate font-semibold">{{ lane.folded }}</span>
            <span class="ml-auto flex-none text-muted-foreground" aria-hidden="true">{{ folded(lane) ? "▸" : "▾" }}</span>
            <span v-if="folded(lane)" class="flex w-full flex-wrap gap-1 pl-[18px]">
              <span v-for="n in lane.nodes" :key="n.id" data-chip :class="['rounded-md border px-1.5 py-px text-[11px]', n.status === 'skipped' ? 'border-dashed text-muted-foreground' : 'border-primary/45']">{{ n.label }}</span>
            </span>
          </button>
        </template>

        <template v-if="!folded(lane)">
          <template v-for="n in lane.nodes" :key="n.id">
            <!-- the open node: its jobs, and the running job's inner steps under it -->
            <div v-if="n.id === openId" data-open :data-node="n.id" :data-status="n.status" class="my-1.5 rounded-[10px] border border-info-foreground px-2">
              <div :class="[rowCls, 'border-b-0']" data-head>
                <span :class="['size-2 flex-none rounded-full', DOT[n.status], n.status === 'running' ? 'motion-safe:animate-pulse' : '']" />
                <span class="min-w-0 truncate font-semibold">{{ n.label }}<template v-if="n.title"> <small class="font-normal text-muted-foreground">· {{ n.title }}</small></template></span>
                <span v-for="(t, i) in n.tags ?? []" :key="i" :class="['flex-none rounded-md px-1.5 py-px text-[11px] font-bold', TAG[t.tone]]">{{ t.text }}</span>
              </div>
              <template v-for="(r, i) in rowsOf(n)" :key="i">
                <template v-if="r.kind === 'cycle' && i !== lastCycleIndex(rowsOf(n))">
                  <button type="button" data-earlier :class="[rowCls, 'pl-3 text-muted-foreground']" @click="pick(n.id, tail(r.jobs)?.id ?? 'build')">
                    <span :class="['size-2 flex-none rounded-[2px]', MARK[r.status]]" />
                    <span class="min-w-0 truncate">{{ earlierText(rowsOf(n), i) }}</span>
                    <span class="ml-auto flex-none" aria-hidden="true">▸</span>
                  </button>
                </template>
                <template v-else-if="r.kind === 'cycle'">
                  <template v-for="j in r.jobs" :key="j.id">
                    <button type="button" data-job-row :data-job="j.id" :data-status="j.status" :aria-pressed="isSelected(n.id, j.id) ? 'true' : 'false'" :class="[rowCls, 'pl-3', isSelected(n.id, j.id) ? 'ring-2 ring-ring' : '']" @click="pick(n.id, j.id)">
                      <span :class="['size-2 flex-none rounded-[2px]', MARK[j.status]]" />
                      <span :class="['min-w-0 truncate', j.status === 'pending' ? 'text-muted-foreground' : '']">{{ j.label }}</span>
                      <span class="ml-auto flex-none font-mono text-xs text-muted-foreground">{{ sub(j) }}</span>
                    </button>
                    <div v-if="steps(j).length" data-steps class="flex flex-col gap-0.5 pb-1.5 pl-7">
                      <div v-for="(s, k) in steps(j)" :key="k" data-step :data-current="s.status === 'running' ? '' : undefined" :class="['flex min-w-0 items-center gap-2 rounded-md px-1.5 py-1 text-xs', s.status === 'running' ? 'text-info-foreground outline outline-2 -outline-offset-1 outline-info-foreground' : s.status === 'pending' ? 'text-muted-foreground' : '']">
                        <span :class="['size-2 flex-none rounded-[2px]', MARK[s.status] ?? 'bg-track']" />
                        <span class="min-w-0 truncate">{{ s.text }}</span>
                        <span v-if="s.seconds != null" class="ml-auto flex-none font-mono text-[11px] text-muted-foreground">{{ briefSpan(s.seconds) }}</span>
                      </div>
                    </div>
                  </template>
                </template>
                <div v-else-if="!absorbed(rowsOf(n), i)" data-divider class="truncate py-1 pl-3 text-xs text-muted-foreground">{{ r.text }}</div>
              </template>
            </div>

            <!-- a layer closed: one row; with several tries, a tap unfolds every try and divider -->
            <template v-else-if="n.kind === 'layer'">
              <component
                :is="n.jobs.length ? 'button' : 'div'"
                :type="n.jobs.length ? 'button' : undefined"
                :data-node="n.id"
                :data-status="n.status"
                :aria-expanded="tryCount(n) > 1 ? !!triesOpen[n.id] : undefined"
                :aria-pressed="n.jobs.length && tryCount(n) <= 1 ? (selected?.[0] === n.id ? 'true' : 'false') : undefined"
                :class="[rowCls, 'truncate', selected?.[0] === n.id ? 'ring-2 ring-ring' : '']"
                @click="tryCount(n) > 1 ? toggleTries(n.id) : n.jobs.length && pick(n.id, tail(n.jobs).id)"
              >
                <span :class="['size-2 flex-none rounded-full', DOT[n.status]]" />
                <strong :class="['flex-none', n.status === 'pending' ? 'text-muted-foreground' : '']">{{ n.label }}</strong>
                <span :title="n.title ?? ''" class="min-w-0 truncate text-xs text-muted-foreground">{{ n.title }}</span>
                <span v-if="tryCount(n) > 1" data-tries class="flex-none rounded-md bg-chip px-1.5 py-px text-[11px] font-bold text-chip-foreground">{{ tryCount(n) }} tries</span>
                <span v-for="(t, i) in n.tags ?? []" :key="i" :class="['flex-none rounded-md px-1.5 py-px text-[11px] font-bold', TAG[t.tone]]">{{ t.text }}</span>
              </component>
              <div v-if="tryCount(n) > 1 && triesOpen[n.id]" data-tries-list class="flex flex-col pl-3">
                <template v-for="(r, i) in rowsOf(n)" :key="i">
                  <button v-if="r.kind === 'cycle'" type="button" data-try :data-row-id="r.id" :class="[rowCls, 'text-muted-foreground']" @click="pick(n.id, tail(r.jobs)?.id ?? 'build')">
                    <span :class="['size-2 flex-none rounded-[2px]', MARK[r.status]]" />
                    <span class="min-w-0 truncate">{{ r.label }} · {{ WORD[r.status] ?? r.status }}</span>
                    <span class="ml-auto flex-none" aria-hidden="true">▸</span>
                  </button>
                  <div v-else data-divider class="truncate py-1 text-xs text-muted-foreground">{{ r.text }}</div>
                </template>
              </div>
            </template>

            <!-- a stage closed: a row per job -->
            <template v-else>
              <button
                v-for="(j, k) in n.jobs"
                :key="j.id"
                type="button"
                :data-node="n.id"
                :data-job="j.id"
                :data-status="j.status"
                :aria-pressed="isSelected(n.id, j.id) ? 'true' : 'false'"
                :class="[rowCls, 'truncate', k ? 'pl-5' : '', isSelected(n.id, j.id) ? 'ring-2 ring-ring' : '']"
                @click="pick(n.id, j.id)"
              >
                <span :class="['size-2 flex-none rounded-full', DOT[j.status]]" />
                <span :class="['min-w-0 truncate font-semibold', j.status === 'pending' ? 'text-muted-foreground' : '']">{{ k ? j.label : n.label }}</span>
                <span class="ml-auto flex-none font-mono text-xs text-muted-foreground">{{ sub(j) }}</span>
              </button>
            </template>
          </template>
        </template>
      </template>
    </div>

    <ExitRow v-if="exit" :exit="exit" :selected="selected" @pick="pick" />
  </div>
</template>
