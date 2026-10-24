<script setup lang="ts">
import { computed } from "vue";
import type { Item } from "@/api/board";
import ItemKey from "@/components/ItemKey.vue";
import TypeTag from "@/components/TypeTag.vue";

const props = defineProps<{ item: Item; fresh: boolean }>();
const emit = defineEmits<{ open: [key: string] }>();

/** The repository label (`repo:<owner>/<name>`) first, then any other. */
const labels = computed(() => [...props.item.labels].sort((a, b) => Number(b.startsWith("repo:")) - Number(a.startsWith("repo:"))));

function dragstart(e: DragEvent): void {
  if (!e.dataTransfer) return;
  e.dataTransfer.setData("text/plain", props.item.key);
  e.dataTransfer.effectAllowed = "move";
}
</script>

<template>
  <article
    :data-item="item.key"
    draggable="true"
    tabindex="0"
    :class="[
      'flex min-w-0 cursor-grab flex-col gap-1.5 rounded-xl border bg-card px-3 py-[11px] shadow-card outline-none focus-visible:ring-2 focus-visible:ring-ring',
      fresh ? 'border-transparent ring-2 ring-ring' : 'border-border',
    ]"
    @click="emit('open', item.key)"
    @keydown.enter="emit('open', item.key)"
    @dragstart="dragstart"
  >
    <div class="flex min-w-0 items-center justify-between gap-2">
      <ItemKey data-key :value="item.key" />
      <TypeTag :type="item.level" />
    </div>
    <div data-title :title="item.title" class="line-clamp-3 break-words font-medium leading-[1.35] [overflow-wrap:anywhere]">{{ item.title }}</div>
    <span
      v-for="l in labels"
      :key="l"
      data-label
      :title="l"
      class="max-w-full self-start truncate rounded-md bg-chip px-[7px] py-px text-[11px] text-chip-foreground"
      >{{ l }}</span
    >
    <div v-if="fresh" data-fresh-note class="flex items-center gap-1.5 text-xs text-ok-foreground">updated just now</div>
  </article>
</template>
