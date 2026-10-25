<script setup lang="ts">
import { RouterLink, useRouter } from "vue-router";
import type { StoryRow } from "@/api/workflows";
import EmptyState from "@/components/EmptyState.vue";
import ItemKey from "@/components/ItemKey.vue";
import StatusChip from "@/components/StatusChip.vue";
import { ago } from "@/screens/board/time";
import { computed } from "vue";
import { repoLabels as makeLabels } from "./repoLabel";

const props = withDefaults(defineProps<{ stories: StoryRow[]; empty: string; sorted?: string; repoLabels?: Map<string, string> }>(), { sorted: "Last activity ↓", repoLabels: undefined });
const labels = computed(() => props.repoLabels ?? makeLabels(props.stories.flatMap((s) => (s.repo ? [s.repo] : []))));

const router = useRouter();
const href = (s: StoryRow) => `/workflows/${encodeURIComponent(s.key)}`;
/** The row opens its story; a click that is on a link already did. */
function open(e: MouseEvent, s: StoryRow) {
  if ((e.target as HTMLElement).closest("a")) return;
  void router.push(href(s));
}

// one grid serves the desktop table and the phone list: below 640 px a row wraps (key, stage, time, then the title)
const GRID = "grid grid-cols-[90px_minmax(0,1fr)_150px_170px_120px] gap-x-4 max-[640px]:flex max-[640px]:flex-wrap max-[640px]:items-center max-[640px]:gap-x-2 max-[640px]:gap-y-1";
</script>

<template>
  <div>
    <div role="row" :class="[GRID, 'border-b border-border px-[18px] py-2.5 text-xs font-semibold text-muted-foreground max-[640px]:hidden']">
      <span>Key</span><span>Title</span><span>Stage</span><span>Repository</span><span class="text-foreground">{{ sorted }}</span>
    </div>
    <EmptyState v-if="!stories.length" :text="empty" />
    <div
      v-for="s in stories"
      :key="s.key"
      :data-story="s.key"
      :class="[GRID, 'cursor-pointer border-b border-border px-[18px] py-[11px] text-sm hover:bg-muted/50 max-[640px]:px-4 max-[640px]:py-3']"
      @click="open($event, s)"
    >
      <RouterLink :to="href(s)" class="block min-w-0 truncate max-[640px]:order-1"><ItemKey :value="s.key" /></RouterLink>
      <div class="min-w-0 max-[640px]:order-4 max-[640px]:basis-full">
        <RouterLink :to="href(s)" :title="s.title ?? s.key" class="block truncate">{{ s.title ?? s.key }}</RouterLink>
        <div v-if="s.blocked" class="mt-[3px] break-words text-xs text-danger-foreground">Blocked: {{ s.blocked }}</div>
      </div>
      <div class="max-[640px]:order-2"><StatusChip kind="stage" :value="s.blocked ? 'blocked' : (s.stage ?? 'new')" /></div>
      <div v-if="s.repo" :title="s.repo" class="min-w-0 truncate text-muted-foreground max-[640px]:hidden">{{ labels.get(s.repo) ?? s.repo }}</div>
      <div v-else class="max-[640px]:hidden" />
      <div class="whitespace-nowrap text-muted-foreground max-[640px]:order-3 max-[640px]:ml-auto max-[640px]:text-xs">{{ ago(s.last_activity) }}</div>
    </div>
  </div>
</template>
