<script setup lang="ts">
import type { FlowStatus } from "@/api/workflows";
import { DOT } from "./style";

/** A point the story passes (PLANNED, PUBLISHED): a diamond, its name and the time. */
defineProps<{ label: string; status: FlowStatus; at?: string | null }>();
</script>

<template>
  <div data-shape="milestone" :data-status="status" class="flex flex-none flex-col items-center gap-1 px-1">
    <span data-diamond :class="['size-3.5 rotate-45 rounded-[2px]', DOT[status]]"></span>
    <b class="text-[11px] tracking-[0.06em]">{{ label }}</b>
    <span v-if="at" class="font-mono text-[11px] text-muted-foreground">{{ at }}</span>
  </div>
</template>
