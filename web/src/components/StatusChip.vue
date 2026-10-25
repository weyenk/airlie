<script setup lang="ts">
import { computed } from "vue";
import { chipSpec, FAMILY_CLASS, type ChipKind } from "./statusTable";

const props = defineProps<{ kind: ChipKind; value: string }>();
const spec = computed(() => chipSpec(props.kind, props.value));
</script>

<template>
  <span
    :class="['inline-flex items-center gap-[5px] whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-bold', FAMILY_CLASS[spec.family]]"
  >
    <span v-if="spec.dot" class="size-[7px] rounded-full bg-current" />{{ spec.label }}
  </span>
</template>
