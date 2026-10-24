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
const isOurs = (e: DragEvent): boolean => !!e.dataTransfer && Array.from(e.dataTransfer.types ?? []).includes(DRAG_TYPE);

/**
 * Where the pointer says the card would land: above the card under it, or the bottom over the strip below the
 * last card. Over anything else (the 8px gaps, the landing mark, which takes no pointer so the list is what is
 * hit there) the place stays as it is, so the mark never jumps; only a drag that has no place yet falls back to
 * the pointer's height against the cards' middles, wherever in the column it entered (the header, the padding).
 */
function placeFor(e: DragEvent): string | null | undefined {
  const el = target(e);
  const card = cardUnder(e);
  if (card !== null) return card;
  if (el?.closest("[data-tail]")) return null;
  if (landing.value !== undefined || typeof e.clientY !== "number" || !Number.isFinite(e.clientY)) return landing.value;
  for (const c of (e.currentTarget as HTMLElement | null)?.querySelectorAll<HTMLElement>("[data-list] [data-item]") ?? []) {
    const r = c.getBoundingClientRect();
    if (e.clientY < r.top + r.height / 2) return c.dataset.item ?? null;
  }
  return null;
}

function over(e: DragEvent): void {
  if (!isOurs(e)) return; // a foreign drag is not ours to take
  e.preventDefault(); // allow the drop
  if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
  landing.value = placeFor(e);
}
function leave(e: DragEvent): void {
  // moving between this column's own children also fires dragleave; only leaving the column clears the mark
  if (!(e.relatedTarget instanceof Node && (e.currentTarget as Node).contains(e.relatedTarget))) landing.value = undefined;
}
function drop(e: DragEvent): void {
  if (!isOurs(e)) return;
  e.preventDefault();
  const key = e.dataTransfer?.getData(DRAG_TYPE);
  const before = placeFor(e) ?? null;
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
