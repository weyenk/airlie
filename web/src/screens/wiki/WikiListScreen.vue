<script setup lang="ts">
import { computed } from "vue";
import { buildEntries, type WikiEntry } from "@/wiki";

const entries = computed(() => buildEntries());
// folders in first-seen order, each with its entries in file order
const sections = computed(() => {
  const byFolder = new Map<string, WikiEntry[]>();
  for (const e of entries.value) {
    const list = byFolder.get(e.folder) ?? [];
    list.push(e);
    byFolder.set(e.folder, list);
  }
  return [...byFolder.entries()];
});
</script>

<template>
  <div class="mx-auto max-w-2xl space-y-8 p-6">
    <h1 class="text-xl font-bold">Wiki</h1>
    <section v-for="[folder, items] in sections" :key="folder" class="space-y-2">
      <h2>{{ folder }}</h2>
      <ul class="space-y-1">
        <li v-for="e in items" :key="e.page">
          <RouterLink :to="`/wiki/${folder}/${e.page}`">{{ e.title }}</RouterLink>
        </li>
      </ul>
    </section>
  </div>
</template>
