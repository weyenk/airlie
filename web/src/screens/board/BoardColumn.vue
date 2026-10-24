<script setup lang="ts">
import { computed, ref } from "vue";
import type { Item, Status } from "@/api/board";
import EmptyState from "@/components/EmptyState.vue";
import StatusChip from "@/components/StatusChip.vue";
import { chipSpec } from "@/components/statusTable";
import ItemCard from "./ItemCard.vue";
import { DRAG_TYPE } from "./statuses";

const props = defineProps<{ status: Status; items: Item[]; changed: Set<string>; active: boolean }>();
const emit = defineEmits<{ move: [key: string, status: Status, before: string | null] }>();

const label = computed(() => chipSpec("board", props.status).label);
/** Where a dragged card would land: above this card, at the bottom (null), or nowhere (undefined: no drag over). */
const landing = ref<string | null | undefined>(undefined);

const MARK = "h-[46px] shrink-0 rounded-[10px] border-2 border-dashed border-ring opacity-60 pointer-events-none";

const target = (e: Event) => e.target as HTMLElement | null;
const cardUnder = (e: Event): string | null => target(e)?.closest<HTMLElement>("[data-item]")?.dataset.item ?? null;
/** The landing mark sits under the pointer once shown; it must not read as "empty space" and move itself. */
const onMark = (e: Event): boolean => !!target(e)?.closest("[data-drop]");
const isOurs = (e: DragEvent): boolean => !!e.dataTransfer && Array.from(e.dataTransfer.types ?? []).includes(DRAG_TYPE);

function over(e: DragEvent): void {
  if (!isOurs(e)) return; // a foreign drag is not ours to take
  e.preventDefault(); // allow the drop
  if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
  if (!onMark(e)) landing.value = cardUnder(e);
}
function leave(e: DragEvent): void {
  // moving between this column's own children also fires dragleave; only leaving the column clears the mark
  if (!(e.relatedTarget instanceof Node && (e.currentTarget as Node).contains(e.relatedTarget))) landing.value = undefined;
}
function drop(e: DragEvent): void {
  if (!isOurs(e)) return;
  e.preventDefault();
  const key = e.dataTransfer?.getData(DRAG_TYPE);
  // dropped on the mark: where it shows, not "the bottom"
  const before = onMark(e) ? (landing.value ?? null) : cardUnder(e);
  landing.value = undefined;
  if (!key || key === before) return;
  const at = props.items.findIndex((i) => i.key === key);
  if (at >= 0 && before === (props.items[at + 1]?.key ?? null)) return; // already exactly there
  emit("move", key, props.status, before);
}
</script>

<template>
  <section
    :aria-label="label"
    :data-status="status"
    :class="['flex min-h-0 min-w-0 flex-col gap-2 rounded-xl border border-border bg-muted/45 py-2.5 pr-1.5 pl-2.5', active ? '' : 'max-[640px]:hidden']"
    @dragover="over"
    @dragleave="leave"
    @drop="drop"
  >
    <div class="flex items-center justify-between px-0.5 pb-1">
      <StatusChip kind="board" :value="status" />
      <span data-count class="text-xs font-semibold text-muted-foreground">{{ items.length }}</span>
    </div>
    <div data-list class="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto pr-1 [scrollbar-width:thin]">
      <template v-for="i in items" :key="i.key">
        <div v-if="landing === i.key" data-drop :class="MARK" />
        <ItemCard :item="i" :fresh="changed.has(i.key)" />
      </template>
      <div v-if="landing === null" data-drop :class="MARK" />
      <EmptyState v-if="!items.length && landing === undefined" text="Nothing here" />
      <!-- the strip below the last card: the bottom stays reachable when the column is full -->
      <div data-tail class="min-h-6 flex-1" />
    </div>
  </section>
</template>
