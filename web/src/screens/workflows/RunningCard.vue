<script setup lang="ts">
import { ChevronRight } from "lucide-vue-next";
import { RouterLink } from "vue-router";
import type { RunningRow } from "@/api/workflows";
import EmptyState from "@/components/EmptyState.vue";
import ItemKey from "@/components/ItemKey.vue";
import ProgressBar from "@/components/ProgressBar.vue";
import StatusChip from "@/components/StatusChip.vue";

defineProps<{ rows: RunningRow[] }>();

const href = (r: RunningRow) => (r.kind === "run" ? `/workflows/runs/${encodeURIComponent(r.key)}` : `/workflows/${encodeURIComponent(r.key)}`);
</script>

<template>
  <section aria-label="Running" class="rounded-xl border border-border bg-card shadow-sm">
    <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3.5">
      <h2 class="text-[15px] font-semibold">Running</h2>
      <span data-count class="rounded-full bg-muted px-2 py-px text-xs font-semibold text-muted-foreground">{{ rows.length }}</span>
    </div>
    <EmptyState v-if="!rows.length" text="Nothing is running" />
    <RouterLink
      v-for="r in rows"
      :key="`${r.kind}:${r.key}`"
      :to="href(r)"
      :data-running="r.key"
      class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3.5 gap-y-2 border-b border-border px-[18px] py-3.5 last:border-b-0 hover:bg-muted/50 max-[640px]:px-4 max-[640px]:py-3"
    >
      <div class="flex min-w-0 items-baseline gap-2.5">
        <ItemKey v-if="r.kind === 'story'" :value="r.key" />
        <StatusChip v-else kind="run" :value="r.topology ?? 'single'" />
        <span :title="r.title ?? r.key" class="truncate font-medium">{{ r.title ?? r.key }}</span>
      </div>
      <ChevronRight class="row-span-2 size-4 text-muted-foreground" />
      <ProgressBar :value="r.progress === null ? null : r.progress * 100" />
    </RouterLink>
  </section>
</template>
