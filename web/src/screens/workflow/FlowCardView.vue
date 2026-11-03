<script setup lang="ts">
import type { FlowCard } from "@/api/workflows";
import { briefSpan } from "./format";
import { MARK, TAG } from "./style";

defineProps<{ card: FlowCard }>();
</script>

<template>
  <div data-card class="flex w-[290px] max-w-[calc(100vw-32px)] flex-col gap-[7px] rounded-[10px] border bg-card p-[10px_12px] text-[12.5px] text-foreground shadow-lg">
    <h5 class="m-0 flex flex-wrap items-center gap-1.5 text-[13px] font-semibold">
      {{ card.title }}
      <span v-for="(t, i) in card.tags" :key="i" :class="['rounded-md px-1.5 py-px text-[11px] font-bold', TAG[t.tone]]">{{ t.text }}</span>
    </h5>
    <template v-for="(row, i) in card.rows" :key="i">
      <div v-if="row.kind === 'squares'" data-row="squares" class="flex items-center gap-2">
        <span class="w-[54px] flex-none font-mono text-[10.5px] text-muted-foreground">{{ row.label }}</span>
        <span class="flex flex-wrap items-center gap-[3px]">
          <span v-for="(m, j) in row.marks" :key="j" data-mark :class="['size-[9px] rounded-[2px]', MARK[m] ?? 'bg-track']"></span>
        </span>
        <span v-if="row.text" class="font-mono text-[11px] text-muted-foreground">{{ row.text }}</span>
      </div>
      <div v-else-if="row.kind === 'step'" data-row="step" class="grid grid-cols-[10px_1fr_auto] items-center gap-[7px] text-xs">
        <span :class="['size-2 rounded-[1px]', MARK[row.status] ?? 'bg-track']"></span>
        <span :class="row.status === 'running' ? 'text-info-foreground' : row.status === 'pending' ? 'text-muted-foreground' : ''">{{ row.text }}</span>
        <span v-if="row.seconds != null" class="font-mono text-[11px] text-muted-foreground">{{ briefSpan(row.seconds) }}</span>
      </div>
      <div v-else-if="row.kind === 'text'" data-row="text" class="flex items-center gap-2">
        <span class="w-[54px] flex-none font-mono text-[10.5px] text-muted-foreground">{{ row.label }}</span>
        <span>{{ row.text }}</span>
      </div>
      <div v-else data-row="votes" class="grid grid-cols-[54px_repeat(3,1fr)] gap-x-1.5 gap-y-0.5 font-mono text-[11px]">
        <span></span>
        <span class="text-[10px] text-muted-foreground">req</span>
        <span class="text-[10px] text-muted-foreground">changes</span>
        <span class="text-[10px] text-muted-foreground">proof</span>
        <template v-for="v in row.votes" :key="v.role">
          <span>{{ v.role }}</span>
          <span>{{ v.requirements ?? "·" }}</span>
          <span>{{ v.changes ?? "·" }}</span>
          <span>{{ v.proof ?? "·" }}</span>
        </template>
      </div>
    </template>
    <div v-if="card.foot" class="border-t pt-1.5 text-[11.5px] text-muted-foreground">{{ card.foot }}</div>
  </div>
</template>
