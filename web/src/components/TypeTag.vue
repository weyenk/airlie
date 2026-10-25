<script setup lang="ts">
import { TYPE_COLOURS, UNKNOWN_TYPE_COLOUR } from "./typeColours";

defineProps<{ type: string }>();
</script>

<template>
  <span class="inline-flex items-center gap-1.5 whitespace-nowrap rounded-md bg-chip px-2 py-0.5 text-xs font-bold text-chip-foreground">
    <span data-square class="size-2 rounded-[2px]" :style="{ background: Object.hasOwn(TYPE_COLOURS, type) ? TYPE_COLOURS[type] : UNKNOWN_TYPE_COLOUR }" />{{ type }}
  </span>
</template>
