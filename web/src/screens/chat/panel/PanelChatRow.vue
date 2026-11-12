<script setup lang="ts">
import type { ThreadRow } from "@/api/sessions";

defineProps<{ session: ThreadRow; current: boolean }>();
</script>

<template>
  <button
    type="button"
    :aria-current="current ? 'page' : undefined"
    :class="[
      'flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-muted',
      current ? 'bg-muted font-semibold' : '',
    ]"
  >
    <span class="flex items-center gap-2">
      <span class="min-w-0 grow truncate">{{ session.title || "New chat" }}</span>
      <span v-if="session.running" role="img" aria-label="Running" class="size-1.5 shrink-0 rounded-full bg-primary" />
    </span>
    <span v-if="session.kind === 'repository'" class="truncate text-xs font-normal text-muted-foreground">
      {{ session.repo }}, {{ session.branch }}
    </span>
  </button>
</template>
