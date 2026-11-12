<script setup lang="ts">
import { Check } from "lucide-vue-next";
import { computed } from "vue";
import type { TodoItem } from "@/api/sessions";

/** The model's plan: each item with its status, and how many are done. */
const props = defineProps<{ items: TodoItem[] }>();
const done = computed(() => props.items.filter((i) => i.status === "done").length);
const LABEL = { pending: "Pending", in_progress: "In progress", done: "Done" } as const;
</script>

<template>
  <section data-todos aria-label="Plan" class="overflow-hidden rounded-lg border border-border bg-card">
    <header class="flex items-baseline gap-2 border-b border-border px-3 py-2 text-xs">
      <strong>Plan</strong><span class="text-muted-foreground">{{ done }} of {{ items.length }} done</span>
    </header>
    <ul class="m-0 flex list-none flex-col gap-1 px-3 py-2 text-sm">
      <li v-for="item in items" :key="item.id" :data-status="item.status" class="flex items-start gap-2">
        <span
          aria-hidden="true"
          :class="[
            'mt-0.5 inline-flex size-4 flex-none items-center justify-center rounded-[4px] border',
            item.status === 'done' ? 'border-primary bg-primary text-primary-foreground' : item.status === 'in_progress' ? 'border-primary' : 'border-border',
          ]"
        >
          <Check v-if="item.status === 'done'" class="size-3" />
          <span v-else-if="item.status === 'in_progress'" class="size-1.5 rounded-full bg-primary" />
        </span>
        <span :class="item.status === 'done' && 'text-muted-foreground line-through'"><span class="sr-only">{{ LABEL[item.status] }}: </span>{{ item.text }}</span>
      </li>
    </ul>
  </section>
</template>
