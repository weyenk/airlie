<script setup lang="ts">
import { useMediaQuery } from "@vueuse/core";
import { computed } from "vue";
import type { FlowNode } from "@/api/workflows";
import { briefSpan } from "./format";
import JobBox from "./JobBox.vue";
import LayerLane from "./LayerLane.vue";
import { BOX, DOT } from "./style";

defineOptions({ name: "AirlieWorkflowPipeline" });

const props = defineProps<{
  nodes: FlowNode[];
  selected: [string, string] | null;
  /** 0 to 100 when the server knows (the running list); else it is worked out from the layers built. */
  progress?: number | null;
}>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();

const phone = useMediaQuery("(max-width: 639px)");

type Item = { kind: "stage"; node: FlowNode } | { kind: "lanes"; nodes: FlowNode[] };
/** The nodes in order, the layers gathered into one block of lanes; a stage that never ran is left out. */
const items = computed<Item[]>(() => {
  const out: Item[] = [];
  for (const node of props.nodes) {
    if (node.kind === "layer") {
      const last = out[out.length - 1];
      if (last?.kind === "lanes") last.nodes.push(node);
      else out.push({ kind: "lanes", nodes: [node] });
    } else if (node.jobs.length) out.push({ kind: "stage", node });
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
const SHORT: Record<string, string> = { tests: "Tests", checks: "Checks", build: "Build" };
const sub = (n: FlowNode) => {
  const j = n.jobs[0];
  return [j.role, j.duration_s ? briefSpan(j.duration_s) : null].filter(Boolean).join(" · ") || j.status;
};
</script>

<template>
  <section aria-label="Pipeline" class="rounded-xl border border-border bg-card shadow-sm">
    <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
      <h2 class="text-[15px] font-semibold">Pipeline</h2>
      <span class="grow" />
      <span v-if="figure && !phone" data-progress class="text-xs text-muted-foreground">{{ figure }}</span>
    </div>

    <div v-if="!phone" data-flow class="flex items-center overflow-x-auto p-[18px] [mask-image:linear-gradient(to_right,#000_96%,transparent)]">
      <template v-for="(item, i) in items" :key="item.kind === 'stage' ? item.node.id : 'lanes'">
        <span v-if="i" class="flex-none px-2 text-muted-foreground" aria-hidden="true">→</span>
        <JobBox
          v-if="item.kind === 'stage'"
          :node-id="item.node.id"
          :job="item.node.jobs[0]"
          :selected="isSelected(item.node.id, item.node.jobs[0].id)"
          @pick="(n, j) => emit('pick', n, j)"
        />
        <div v-else class="flex flex-none flex-col gap-2">
          <LayerLane v-for="n in item.nodes" :key="n.id" :node="n" :selected="selected" @pick="(a, b) => emit('pick', a, b)" />
        </div>
      </template>
    </div>

    <div v-else class="flex flex-col px-3 pt-2 pb-3">
      <template v-for="item in items" :key="item.kind === 'stage' ? item.node.id : 'lanes'">
        <button
          v-if="item.kind === 'stage'"
          type="button"
          data-vstage
          :data-job="item.node.id"
          :data-node="item.node.id"
          :data-status="item.node.jobs[0].status"
          :aria-pressed="isSelected(item.node.id, item.node.jobs[0].id) ? 'true' : 'false'"
          :class="['flex items-center gap-2.5 border-b border-border px-1 py-[9px] text-left last:border-b-0', isSelected(item.node.id, item.node.jobs[0].id) ? 'ring-2 ring-ring' : '', item.node.jobs[0].status === 'running' ? 'motion-safe:animate-pulse' : '']"
          @click="emit('pick', item.node.id, item.node.jobs[0].id)"
        >
          <span :class="['size-2 flex-none rounded-full', DOT[item.node.jobs[0].status]]" />
          <span :class="['font-semibold', item.node.jobs[0].status === 'pending' ? 'text-muted-foreground' : '']">{{ item.node.label }}</span>
          <span class="ml-auto font-mono text-xs text-muted-foreground">{{ sub(item.node) }}</span>
        </button>
        <template v-else>
          <div
            v-for="n in item.nodes"
            :key="n.id"
            data-vlane
            :data-node="n.id"
            :data-status="n.status"
            :class="['my-2 min-w-0 rounded-[10px] border px-2.5 py-2', n.status === 'running' ? 'border-info-foreground' : 'border-border']"
          >
            <div class="flex min-w-0 items-center gap-2">
              <span :class="['size-2 flex-none rounded-full', DOT[n.status]]" />
              <strong :class="['flex-none', n.status === 'pending' ? 'text-muted-foreground' : '']">{{ n.label }}</strong>
              <span :title="n.title ?? ''" class="min-w-0 truncate text-xs text-muted-foreground">{{ n.title }}</span>
            </div>
            <div data-jobs class="mt-1.5 flex gap-1.5">
              <button
                v-for="j in n.jobs"
                :key="j.id"
                type="button"
                :data-job="j.id"
                :data-status="j.status"
                :aria-pressed="isSelected(n.id, j.id) ? 'true' : 'false'"
                :class="['min-w-0 flex-1 truncate rounded-md border bg-card py-[5px] text-center text-xs', BOX[j.status], isSelected(n.id, j.id) ? 'ring-2 ring-ring' : '', j.status === 'running' ? 'motion-safe:animate-pulse' : '']"
                @click="emit('pick', n.id, j.id)"
              >
                {{ (SHORT[j.id] ?? j.label) + (j.attempts > 1 ? ` ${j.attempts}x` : "") }}
              </button>
            </div>
          </div>
        </template>
      </template>
    </div>
  </section>
</template>
