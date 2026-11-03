import { computed, ref, watch } from "vue";
import type { Ref } from "vue";
import { useRoute } from "vue-router";
import type { Flow, FlowJob } from "@/api/workflows";

type Pick = readonly [string, string, string];
const param = (v: unknown) => (typeof v === "string" && v ? v : undefined);
const jobsOf = (nodes: Flow["nodes"]) => nodes.flatMap((n) => n.jobs.map((j) => [n.id, j.id, j.status] as const));
/** Every [node, job] a box can open: the shown jobs, each earlier try cycle's jobs, and the exit row's (under node "exit"). */
function pickable(d: Flow | null | undefined): (readonly [string, string])[] {
  if (!d) return [];
  const out: (readonly [string, string])[] = d.nodes.flatMap((n) => [...n.jobs, ...(n.rows ?? []).flatMap((r) => (r.kind === "cycle" ? r.jobs : []))].map((j) => [n.id, j.id] as const));
  for (const j of d.exit?.jobs ?? []) out.push(["exit", j.id]);
  return out;
}
/** The order a box in one node is worth opening in: what runs, what a killed process left open, what failed. */
const WORTH = ["running", "stopped", "failed"] as const;
/**
 * The job a flow opens on: in a story, the running one, else the stopped one (a killed process), else the failed
 * one, else the last that passed (with its status) — a Survey that failed is gone past by design and stays
 * "failed", so failed-first would open a live story on it. A run keeps failed first: its failed chief stays open
 * while its forks go on.
 */
function interesting(d: Flow | null | undefined): Pick | null {
  const all = jobsOf(d?.nodes ?? []);
  for (const s of d?.kind === "run" ? ["failed", "running", "stopped"] : WORTH) {
    const hit = all.find((x) => x[2] === s);
    if (hit) return hit;
  }
  return [...all].reverse().find((x) => x[2] === "passed") ?? null;
}
/** Of one node's jobs, the one a tap opens: running, else stopped, else failed, else the last that ran. */
export function interestingJob(jobs: FlowJob[]): FlowJob | undefined {
  for (const s of WORTH) {
    const hit = jobs.find((j) => j.status === s);
    if (hit) return hit;
  }
  return [...jobs].reverse().find((j) => j.status !== "pending") ?? jobs[jobs.length - 1];
}

/**
 * Which job of a flow (a story's or a run's) is open in the view: the one in the address, else the pinned default,
 * else the last that passed. With no job in the address, a running, stopped or failed default is pinned once chosen, so the
 * view does not jump under a reader as live updates arrive. The last-passed fallback is not pinned: it follows the
 * latest job until something fails or runs. A pinned job that a refresh no longer has (layers re-planned) is
 * dropped and re-picked. Another story or run starts afresh. `data`: undefined while loading, null for none.
 */
export function useJobSelection(id: Ref<string>, data: Ref<Flow | null | undefined>) {
  const route = useRoute();
  const nodes = computed(() => (data.value && data.value.key === id.value ? data.value.nodes : []));

  const pinned = ref<[string, string] | null>(null);
  let pinnedFor: string | undefined;
  watch(
    [id, data],
    ([k, d]) => {
      if (pinnedFor !== k) {
        pinned.value = null;
        pinnedFor = undefined;
      }
      if (!d || d.key !== k) return;
      const have = jobsOf(d.nodes);
      const p = pinned.value;
      if (p && have.some((x) => x[0] === p[0] && x[1] === p[1])) return;
      const pick = interesting(d);
      pinned.value = pick && pick[2] !== "passed" ? [pick[0], pick[1]] : null;
      pinnedFor = k;
    },
    { immediate: true, flush: "sync" },
  );

  const selected = computed<[string, string] | null>(() => {
    const all = jobsOf(nodes.value);
    const node = param(route.params.node);
    const job = param(route.params.job);
    const d = data.value && data.value.key === id.value ? data.value : null;
    if (node && job && pickable(d).some((x) => x[0] === node && x[1] === job)) return [node, job];
    const p = pinned.value;
    if (p && all.some((x) => x[0] === p[0] && x[1] === p[1])) return p;
    const fallback = interesting(d);
    return fallback ? [fallback[0], fallback[1]] : null;
  });
  return { selected, nodes };
}
