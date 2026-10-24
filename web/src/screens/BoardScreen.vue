<script setup lang="ts">
import { ChevronDown, Plus } from "lucide-vue-next";
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import type { Item, Level, Status } from "@/api/board";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useBoardStore } from "@/stores/board";
import BoardColumn from "./board/BoardColumn.vue";
import StatusPills from "./board/StatusPills.vue";
import { STATUSES, TYPE_FILTERS } from "./board/statuses";

const board = useBoardStore();
const router = useRouter();

const type = ref<"all" | Level>("all");
/** The column the phone shows (the pills choose it). */
const phoneStatus = ref<Status>("doing");

const shown = computed<Item[]>(() => (type.value === "all" ? board.items : board.items.filter((i) => i.level === type.value)));
const byStatus = computed(() => {
  const groups = Object.fromEntries(STATUSES.map((s) => [s, [] as Item[]])) as Record<Status, Item[]>;
  for (const i of shown.value) groups[i.status]?.push(i);
  return groups;
});
const counts = computed(() => Object.fromEntries(STATUSES.map((s) => [s, byStatus.value[s].length])) as Record<Status, number>);
const typeLabel = computed(() => TYPE_FILTERS.find((t) => t.value === type.value)?.label ?? "All");

const open = (key: string) => void router.push(`/board/${encodeURIComponent(key)}`);
// Task 7 wires the create modal to this query
const create = () => void router.push({ query: { create: "1" } });
const move = (key: string, status: Status, before: string | null) => void board.move(key, status, before).catch(() => {});
</script>

<template>
  <!-- the window less the 56px header; the 24px bottom padding is the buffer (the phone's tab bar takes 64px more) -->
  <div data-board class="flex h-[calc(100vh-56px)] flex-col gap-4 px-5 pt-6 pb-6 max-[640px]:h-[calc(100vh-120px)] max-[640px]:gap-3.5 max-[640px]:px-3 max-[640px]:pt-4">
    <div class="flex flex-wrap items-center gap-3">
      <h1 class="text-[22px] font-bold tracking-[-0.01em] max-[640px]:text-xl">Board</h1>
      <span class="grow" />
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button data-type-filter variant="outline" class="h-[34px] font-normal text-muted-foreground max-[640px]:hidden">
            Type <strong class="font-medium text-foreground">{{ typeLabel }}</strong>
            <ChevronDown />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuRadioGroup v-model="type">
            <DropdownMenuRadioItem v-for="t in TYPE_FILTERS" :key="t.value" :value="t.value">{{ t.label }}</DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
      <Button data-create class="h-[34px]" @click="create"><Plus />Create</Button>
    </div>
    <ErrorBanner v-if="board.error" :error="board.error" />
    <StatusPills v-model="phoneStatus" :counts="counts" />
    <div data-columns class="grid min-h-0 flex-1 grid-cols-[repeat(6,minmax(190px,1fr))] gap-3 overflow-x-auto max-[640px]:grid-cols-1">
      <BoardColumn
        v-for="s in STATUSES"
        :key="s"
        :status="s"
        :items="byStatus[s]"
        :changed="board.changed"
        :active="s === phoneStatus"
        @move="move"
        @open="open"
      />
    </div>
  </div>
</template>
