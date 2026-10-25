import { computed, ref, watch } from "vue";
import type { Ref } from "vue";
import { useRoute } from "vue-router";
import type { Flow } from "@/api/workflows";

type Pick = readonly [string, string, string];
const param = (v: unknown) => (typeof v === "string" && v ? v : undefined);
const jobsOf = (nodes: Flow["nodes"]) => nodes.flatMap((n) => n.jobs.map((j) => [n.id, j.id, j.status] as const));
/** The failed job, else the running one, else the last that passed (with its status). */
function interesting(nodes: Flow["nodes"]): Pick | null {
  const all = jobsOf(nodes);
  return all.find((x) => x[2] === "failed") ?? all.find((x) => x[2] === "running") ?? [...all].reverse().find((x) => x[2] === "passed") ?? null;
}

/**
 * Which job of a flow (a story's or a run's) is open in the view: the one in the address, else the pinned default,
 * else the last that passed. With no job in the address, a failed or running default is pinned once chosen, so the
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
      const pick = interesting(d.nodes);
      pinned.value = pick && pick[2] !== "passed" ? [pick[0], pick[1]] : null;
      pinnedFor = k;
    },
    { immediate: true, flush: "sync" },
  );

  const selected = computed<[string, string] | null>(() => {
    const all = jobsOf(nodes.value);
    const node = param(route.params.node);
    const job = param(route.params.job);
    if (node && job && all.some((x) => x[0] === node && x[1] === job)) return [node, job];
    const p = pinned.value;
    if (p && all.some((x) => x[0] === p[0] && x[1] === p[1])) return p;
    const fallback = interesting(nodes.value);
    return fallback ? [fallback[0], fallback[1]] : null;
  });
  return { selected, nodes };
}
