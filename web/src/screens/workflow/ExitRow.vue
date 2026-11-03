<script setup lang="ts">
import type { FlowExit } from "@/api/workflows";
import FlowBox from "./FlowBox.vue";
import { TAG } from "./style";

/** Why the story ended where it did: the stop, then the diagnosis and fix card boxes. */
const props = defineProps<{ exit: FlowExit; selected?: [string, string] | null }>();
const emit = defineEmits<{ pick: [node: string, job: string] }>();

const KIND = { blocked: "Blocked", waiting: "Waiting", cancelled: "Cancelled" } as const;
const place = () => (props.exit.kind === "waiting" ? `on ${props.exit.on ?? "another story"}` : props.exit.where);
</script>

<template>
  <div data-exit-row :data-exit="exit.kind" class="flex flex-wrap items-center gap-2.5 border-t border-border px-[18px] py-3">
    <span :class="['flex-none text-[11px] font-bold uppercase tracking-[0.06em]', exit.kind === 'blocked' ? 'text-danger-foreground' : 'text-warn-foreground']">{{ KIND[exit.kind] }}</span>
    <span v-if="place()" class="text-[13px]">{{ place() }}</span>
    <span v-if="exit.times > 1" :class="['rounded-md px-1.5 py-px text-xs font-bold', TAG.warn]">blocked {{ exit.times }} times</span>
    <span v-if="exit.reason" class="min-w-0 truncate text-xs text-muted-foreground">{{ exit.reason }}</span>
    <template v-for="j in exit.jobs" :key="j.id">
      <span class="text-muted-foreground" aria-hidden="true">→</span>
      <FlowBox node-id="exit" :job="j" :selected="selected?.[0] === 'exit' && selected?.[1] === j.id" @pick="(n, jb) => emit('pick', n, jb)" />
    </template>
  </div>
</template>
