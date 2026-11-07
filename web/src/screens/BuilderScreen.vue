<script setup lang="ts">
import { ChevronLeft } from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import { getWorkflowDef } from "@/api/workflow_defs";
import type { WorkflowDetail } from "@/api/workflow_defs";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { useWorkflowDefs } from "./board/workflow";
import Canvas from "./builder/Canvas.vue";
import { emptyGraph, fromWorkflow } from "./builder/graph";
import type { Graph } from "./builder/graph";
import { autoLayout } from "./builder/layout";
import Palette from "./builder/Palette.vue";
import { parseWorkflow, toYaml } from "./builder/yaml";

/** The builder (`/workflows/build/<name>`, or `new` for an empty canvas): the workflow drawn as boxes and wires,
 * the palette, the selected box's fields, and the YAML. Read-only for now; editing comes next. */
const route = useRoute();
const name = computed(() => String(route.params.name ?? ""));

const detail = ref<WorkflowDetail | null>(null);
const graph = ref<Graph>(emptyGraph("new"));
const error = ref<Error | string | null>(null);
const selected = ref<string | null>(null);
const tab = ref<"diagram" | "yaml">("diagram");

let loading = 0;
async function load(wanted: string): Promise<void> {
  const mine = ++loading;
  detail.value = null;
  error.value = null;
  selected.value = null;
  graph.value = emptyGraph(wanted);
  if (wanted === "new") return;
  let def: WorkflowDetail;
  try {
    def = await getWorkflowDef(wanted);
  } catch (e) {
    if (mine === loading) error.value = e instanceof Error ? e : String(e);
    return;
  }
  if (mine !== loading) return; // the address moved on while this one loaded
  detail.value = def;
  try {
    const parsed = parseWorkflow(def.yaml);
    // a box keeps the place the definition saved for it; the rest are laid out left to right
    graph.value = autoLayout(fromWorkflow(parsed.name, parsed.spec, { ...parsed.layout, ...def.layout }));
  } catch (e) {
    graph.value = emptyGraph(def.name);
    error.value = `The YAML could not be drawn: ${e instanceof Error ? e.message : String(e)}`;
  }
}
// the address moving off the builder (a link out, a 401 to sign in) leaves the loaded workflow alone
watch(name, (n) => {
  if (route.name === "builder") void load(n);
}, { immediate: true });

const { defs, failed, reload } = useWorkflowDefs();
const bundles = computed(() => defs.value.map((d) => d.name).filter((n) => n !== name.value));

const title = computed(() => detail.value?.name ?? name.value);
const typeLabel = computed(() => (detail.value ? (detail.value.type ?? "bundle") : null));
const problems = computed(() => detail.value?.problems.length ?? 0);
const yamlText = computed(() => detail.value?.yaml ?? toYaml(graph.value));
const node = computed(() => graph.value.nodes.find((n) => n.id === selected.value) ?? null);

/** A value as it reads in the panel: lists and short maps on one line, as the YAML writes them. */
function inline(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(inline).join(", ")}]`;
  if (typeof v === "object" && v !== null) return `{${Object.entries(v).map(([k, x]) => `${k}: ${inline(x)}`).join(", ")}}`;
  return String(v);
}
function fieldText(v: unknown): string {
  if (Array.isArray(v)) return v.map(inline).join(", ");
  if (typeof v === "object" && v !== null) return Object.entries(v).map(([k, x]) => `${k}: ${inline(x)}`).join("\n");
  return String(v);
}
const fields = computed(() => (node.value ? [["type", node.value.type] as const, ...Object.entries(node.value.spec)].map(([k, v]) => ({ key: k, text: fieldText(v) })) : []));

function pick(id: string) {
  selected.value = selected.value === id ? null : id;
}
const tabClass = (on: boolean) => ["rounded-md px-2.5 py-1 text-[13px] font-medium", on ? "bg-card text-foreground shadow-sm" : "text-muted-foreground"];
</script>

<template>
  <div class="flex flex-col gap-4 px-5 pt-6 pb-10 max-[640px]:gap-3 max-[640px]:px-3 max-[640px]:pt-4">
    <section class="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border bg-card px-[18px] py-3.5 shadow-sm">
      <RouterLink to="/workflows" class="inline-flex items-center gap-1 text-[13px] text-muted-foreground"><ChevronLeft class="size-4" />Workflows</RouterLink>
      <h1 class="font-mono text-lg font-bold">{{ title }}</h1>
      <span v-if="typeLabel" data-workflow-type class="rounded-full bg-chip px-2 py-px text-xs font-semibold text-chip-foreground">{{ typeLabel }}</span>
      <span v-if="detail?.source === 'draft'" class="rounded-full bg-warn px-2 py-px text-xs font-semibold text-warn-foreground">draft</span>
      <span v-if="problems" data-problems class="rounded-full bg-danger px-2 py-px text-xs font-semibold text-danger-foreground">{{ problems }} {{ problems === 1 ? "problem" : "problems" }}</span>
      <a v-if="detail?.pr" :href="detail.pr.url" target="_blank" rel="noopener" class="text-[13px] text-muted-foreground underline">#{{ detail.pr.number }}</a>
      <span class="grow" />
      <div role="tablist" aria-label="View" class="inline-flex gap-0.5 rounded-lg bg-muted p-[3px]">
        <button type="button" role="tab" data-tab="diagram" :aria-selected="tab === 'diagram'" :class="tabClass(tab === 'diagram')" @click="tab = 'diagram'">Diagram</button>
        <button type="button" role="tab" data-tab="yaml" :aria-selected="tab === 'yaml'" :class="tabClass(tab === 'yaml')" @click="tab = 'yaml'">YAML</button>
      </div>
    </section>

    <ErrorBanner v-if="error" :error="error" />

    <pre v-if="tab === 'yaml'" data-yaml class="overflow-auto rounded-xl border border-border bg-[var(--code)] p-4 font-mono text-xs leading-relaxed">{{ yamlText }}</pre>

    <div v-else class="grid grid-cols-[180px_minmax(0,1fr)_260px] items-start gap-3 max-[900px]:grid-cols-1">
      <Palette :bundles="bundles" :failed="failed" @retry="reload" />
      <section aria-label="Canvas" class="max-h-[calc(100dvh-var(--header-h)-200px)] min-h-[240px] overflow-auto rounded-xl border border-border bg-card shadow-sm">
        <Canvas :graph="graph" :selected="selected" readonly @select="pick" />
      </section>
      <aside aria-label="Fields" data-fields class="flex flex-col gap-2 rounded-xl border border-border bg-card p-3 text-[13px] shadow-sm">
        <template v-if="node">
          <h2 class="font-mono text-sm font-semibold">{{ node.id }}</h2>
          <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5">
            <template v-for="f in fields" :key="f.key">
              <dt class="text-muted-foreground">{{ f.key }}</dt>
              <dd :data-field="f.key" class="font-mono text-xs break-words whitespace-pre-line">{{ f.text }}</dd>
            </template>
          </dl>
        </template>
        <template v-else>
          <p class="text-muted-foreground">Select a box to see its fields.</p>
          <dl v-if="graph.start" class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5">
            <dt class="text-muted-foreground">start</dt>
            <dd class="font-mono text-xs">{{ graph.start }}</dd>
            <template v-if="graph.loose.length">
              <dt class="text-muted-foreground">loose</dt>
              <dd class="font-mono text-xs">{{ graph.loose.join(", ") }}</dd>
            </template>
          </dl>
        </template>
      </aside>
    </div>
  </div>
</template>
