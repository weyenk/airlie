<script setup lang="ts">
import { useMediaQuery } from "@vueuse/core";
import { computed } from "vue";
import { PHONE_QUERY } from "@/lib/breakpoints";
import type { FlowExit, FlowNode, FlowNow } from "@/api/workflows";
import ExitRow from "./ExitRow.vue";
import FlowBox from "./FlowBox.vue";
import LayerLane from "./LayerLane.vue";
import NowBar from "./NowBar.vue";
import PhoneList from "./PhoneList.vue";

defineOptions({ name: "AirlieWorkflowPipeline" });

const props = defineProps<{
  nodes: FlowNode[];
  selected: [string, string] | null;
  /** 0 to 100 when the server knows (the running list); else it is worked out from the layers built. */
  progress?: number | null;
  now?: FlowNow;
  exit?: FlowExit | null;
}>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();

const phone = useMediaQuery(PHONE_QUERY);

type Item = { kind: "stage"; node: FlowNode } | { kind: "lanes"; nodes: FlowNode[] };
const keyOf = (item: Item) => (item.kind === "stage" ? item.node.id : "lanes");
/** The nodes in order: a bundle's per-item layers gathered into one block of lanes; a stage that never ran is left out. */
const items = computed<Item[]>(() => {
  const out: Item[] = [];
  for (const node of props.nodes) {
    const last = out[out.length - 1];
    if (node.kind === "layer") {
      if (last?.kind === "lanes") last.nodes.push(node);
      else out.push({ kind: "lanes", nodes: [node] });
    } else if (!node.jobs.length) continue;
    else out.push({ kind: "stage", node });
  }
  return out;
});

const layers = computed(() => props.nodes.filter((n) => n.kind === "layer"));
const figure = computed(() => {
  if (!layers.value.length) return null;
  const built = layers.value.filter((n) => n.status === "passed").length;
  const pct = Math.round(props.progress ?? (built / layers.value.length) * 100);
  return `${pct}% · ${built} of ${layers.value.length} layers built`;
});

const isSelected = (node: string, job: string) => props.selected?.[0] === node && props.selected?.[1] === job;
</script>

<template>
  <section aria-label="Pipeline" class="rounded-xl border border-border bg-card shadow-sm">
    <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
      <h2 class="text-[15px] font-semibold">Pipeline</h2>
      <span class="grow" />
      <span v-if="figure && !phone" data-progress class="text-xs text-muted-foreground">{{ figure }}</span>
    </div>

    <template v-if="!phone">
    <NowBar v-if="now" :now="now" />

    <div data-flow class="flex items-center overflow-x-auto p-[18px] [mask-image:linear-gradient(to_right,#000_96%,transparent)]">
      <template v-for="(item, i) in items" :key="keyOf(item)">
        <span v-if="i" class="flex-none px-2 text-muted-foreground" aria-hidden="true">→</span>
        <div v-if="item.kind === 'stage'" data-stage :data-node="item.node.id" class="flex flex-none flex-col gap-2">
          <FlowBox v-for="j in item.node.jobs" :key="j.id" :node-id="item.node.id" :job="j" :selected="isSelected(item.node.id, j.id)" @pick="(n, jb) => emit('pick', n, jb)" />
        </div>
        <div v-else data-lanes class="flex flex-none flex-col gap-2">
          <LayerLane v-for="n in item.nodes" :key="n.id" :node="n" :selected="selected" @pick="(a, b) => emit('pick', a, b)" />
        </div>
      </template>
    </div>
    <ExitRow v-if="exit" :exit="exit" :selected="selected" @pick="(a, b) => emit('pick', a, b)" />
    </template>

    <PhoneList v-else :nodes="nodes" :now="now" :exit="exit" :selected="selected" @pick="(a, b) => emit('pick', a, b)" />
  </section>
</template>
