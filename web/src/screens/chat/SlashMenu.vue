<script setup lang="ts">
import type { Skill } from "@/api/chat";

defineProps<{ skills: Skill[]; active: number }>();
const emit = defineEmits<{ choose: [skill: Skill] }>();
</script>

<template>
  <div
    data-test="slash-menu"
    class="absolute inset-x-[66px] bottom-[62px] z-10 overflow-hidden rounded-xl border border-border bg-card shadow-[0_12px_30px_rgba(0,0,0,.35)] max-[640px]:inset-x-3"
  >
    <div v-if="!skills.length" data-empty class="px-3.5 py-2 text-muted-foreground">No command matches</div>
    <div v-else role="listbox" aria-label="Commands">
      <div
        v-for="(s, i) in skills"
        :key="s.name"
        role="option"
        :aria-selected="i === active ? 'true' : 'false'"
        :class="['cursor-pointer border-b border-border px-3.5 py-2 last:border-b-0', i === active ? 'bg-muted' : '']"
        @mousedown.prevent="emit('choose', s)"
      >
        <strong class="font-mono font-semibold">/{{ s.name }}</strong>
        <div class="mt-0.5 text-xs text-muted-foreground">{{ s.summary }}</div>
      </div>
    </div>
  </div>
</template>
