<script setup lang="ts">
import type { Status } from "@/api/board";
import { chipSpec } from "@/components/statusTable";
import { STATUSES } from "./statuses";

defineProps<{ counts: Record<Status, number>; modelValue: Status }>();
const emit = defineEmits<{ "update:modelValue": [status: Status] }>();
</script>

<template>
  <div data-pills class="hidden gap-1.5 overflow-x-auto pb-0.5 max-[640px]:flex">
    <button
      v-for="s in STATUSES"
      :key="s"
      type="button"
      :aria-pressed="s === modelValue"
      :class="[
        'inline-flex flex-none items-center gap-1.5 rounded-full border px-2.5 py-[5px] text-[13px]',
        s === modelValue ? 'border-ring font-semibold text-primary' : 'border-border',
      ]"
      @click="emit('update:modelValue', s)"
    >
      {{ chipSpec("board", s).label }} {{ counts[s] }}
    </button>
  </div>
</template>
