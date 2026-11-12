<script setup lang="ts">
import { computed } from "vue";
import { ChartColumn, Cpu, GitBranch, MemoryStick, Terminal } from "lucide-vue-next";
import { sessionStatus } from "@/api/sessions";
import type { ThreadRow } from "@/api/sessions";
import EngineStatusLine from "@/components/EngineStatusLine.vue";
import ProgressBar from "@/components/ProgressBar.vue";
import { useEngines } from "@/stores/engines";
import { liveResource } from "@/stores/live";
import { useTurns } from "@/stores/turns";

/**
 * What the status chip stands for, row by row (the popover's and the phone sheet's body): the VM (a repository chat's),
 * the chat engine, the context, the memory and the session branch. It loads when it opens, and again on each engine and
 * chat change while it is open.
 */
const props = defineProps<{ session: ThreadRow }>();
const engines = useEngines();
const turns = useTurns();
const status = liveResource(["engines", "chat"], () => sessionStatus(props.session.id));

const data = computed(() => status.data.value);
const liveVm = computed(() => engines.byName(`vm:${props.session.id}`));
const liveEngine = computed(() => engines.forRole("chat"));
const usage = computed(() => turns.usage(props.session.id));

const WORDS: Record<string, string> = { ready: "ready", stopped: "stopped", stopping: "stopping", failed: "failed", waiting: "waiting", starting: "starting", downloading: "downloading" };
const k = (tokens: number): string => `${Math.round(tokens / 1000)}k`;
const gb = (n: number): string => (Number.isInteger(n) ? String(n) : n.toFixed(1));

function uptime(since: number): string {
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - since) / 60));
  if (minutes < 1) return "just started";
  if (minutes < 60) return `up ${minutes} min`;
  return `up ${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

const vm = computed(() => {
  const row = data.value?.vm;
  if (!row) return { title: "VM not started", sub: props.session.repo ?? "" };
  const state = liveVm.value?.state ?? row.state;
  const detail = liveVm.value?.detail ?? row.detail;
  const sub = state === "ready" ? `${row.name}, ${uptime(row.since)}` : detail || row.name;
  return { title: `VM ${WORDS[state] ?? state}${props.session.repo ? ` · ${props.session.repo}` : ""}`, sub };
});

const engine = computed(() => {
  const row = data.value?.engine;
  if (!row) return null;
  const state = liveEngine.value?.state ?? row.state;
  const name = liveEngine.value?.name ?? row.name;
  const title = state === "ready" ? `${name} loaded` : state === "stopped" ? `${name} not loaded` : `${name} ${WORDS[state] ?? state}`;
  return { title, sub: `${row.model}, ${gb(row.footprint_gb)} GB`, state };
});

const branch = computed(() => {
  const b = data.value?.branch;
  if (!b) return null;
  let sub = "commits not counted yet";
  if (b.ahead !== null) {
    sub = `${b.ahead} ${b.ahead === 1 ? "commit" : "commits"} ahead of ${b.base}`;
    if (b.pushed !== null) sub += b.pushed ? ", pushed" : ", not pushed";
  }
  return { name: b.name, sub };
});

const row = "flex items-center gap-2.5 px-2 py-2 [&+&]:border-t [&+&]:border-border";
const sub = "text-xs text-muted-foreground";
</script>

<template>
  <div data-status-detail class="flex flex-col text-[13px]">
    <p v-if="status.error.value && !data" class="m-0 px-2 py-2 text-muted-foreground">Status detail is unavailable.</p>
    <p v-else-if="!data" class="m-0 px-2 py-2 text-muted-foreground">Loading…</p>
    <template v-else>
      <div v-if="session.kind === 'repository'" data-row="vm" :class="row">
        <Terminal aria-hidden="true" class="size-4 flex-none text-muted-foreground" />
        <div class="min-w-0">
          <div class="font-semibold">{{ vm.title }}</div>
          <div :class="[sub, 'truncate']">{{ vm.sub }}</div>
          <EngineStatusLine v-if="liveVm && liveVm.state !== 'ready'" :status="liveVm" :label="`a VM for ${session.repo}`" />
        </div>
      </div>
      <div v-if="engine" data-row="engine" :class="row">
        <Cpu aria-hidden="true" class="size-4 flex-none text-muted-foreground" />
        <div class="min-w-0">
          <div class="font-semibold">{{ engine.title }}</div>
          <div :class="[sub, 'truncate font-mono']">{{ engine.sub }}</div>
          <EngineStatusLine v-if="liveEngine && liveEngine.state !== 'ready'" :status="liveEngine" />
        </div>
      </div>
      <div data-row="context" :class="row">
        <ChartColumn aria-hidden="true" class="size-4 flex-none text-muted-foreground" />
        <div class="min-w-0 grow">
          <div class="font-semibold">Context</div>
          <div :class="sub">{{ usage ? `${k(usage.used)} of ${k(usage.limit)} tokens` : "No reply yet" }}</div>
        </div>
        <div v-if="usage" class="w-14 flex-none"><ProgressBar :value="(usage.used / usage.limit) * 100" /></div>
      </div>
      <div data-row="memory" :class="row">
        <MemoryStick aria-hidden="true" class="size-4 flex-none text-muted-foreground" />
        <div class="min-w-0 grow">
          <div class="font-semibold">Memory</div>
          <div :class="sub">
            {{ data.memory.waiting ? data.memory.waiting.charAt(0).toUpperCase() + data.memory.waiting.slice(1) : `${gb(data.memory.used_gb)} of ${gb(data.memory.budget_gb)} GB in use` }}
          </div>
        </div>
        <div v-if="!data.memory.waiting" class="w-14 flex-none"><ProgressBar :value="(data.memory.used_gb / data.memory.budget_gb) * 100" /></div>
      </div>
      <div v-if="branch" data-row="branch" :class="row">
        <GitBranch aria-hidden="true" class="size-4 flex-none text-muted-foreground" />
        <div class="min-w-0">
          <div class="truncate font-semibold">{{ branch.name }}</div>
          <div :class="sub">{{ branch.sub }}</div>
        </div>
      </div>
    </template>
  </div>
</template>
