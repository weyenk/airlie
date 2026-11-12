<script setup lang="ts">
import { ChevronRight } from "lucide-vue-next";
import { ref } from "vue";

/** The model's reasoning: a closed disclosure, opened on click. */
defineProps<{ text: string; streaming: boolean }>();
const open = ref(false);
</script>

<template>
  <div data-reasoning class="text-[13px] text-muted-foreground">
    <button type="button" :aria-expanded="open" class="inline-flex items-center gap-1.5 rounded-md py-0.5 hover:text-foreground" @click="open = !open">
      <ChevronRight aria-hidden="true" :class="['size-3.5 transition-transform', open && 'rotate-90']" />
      <span class="font-semibold">{{ streaming ? "Thinking" : "Reasoning" }}</span>
      <span v-if="streaming" aria-hidden="true" class="animate-pulse">…</span>
    </button>
    <p v-if="open" class="mt-1 ml-5 max-h-64 overflow-y-auto border-l-2 border-border pl-3 whitespace-pre-wrap [overflow-wrap:anywhere]">{{ text }}</p>
  </div>
</template>
