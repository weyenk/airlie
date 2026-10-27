<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import SafeMarkdown from "@/components/SafeMarkdown.vue";
import { getPage, type WikiEntry } from "@/wiki";

const route = useRoute();
const entry = ref<WikiEntry | null>(null);
const loaded = ref(false);

onMounted(async () => {
  entry.value = getPage(String(route.params.folder), String(route.params.page));
  loaded.value = true;
});

// the page's own H1 is the document title; fall back to the derived title
const h1 = (body: string): string | null => {
  for (const line of body.split("\n")) if (line.startsWith("# ")) return line.slice(2);
  return null;
};
const title = computed(() => (entry.value ? h1(entry.value.body) ?? entry.value.title : ""));
</script>

<template>
  <div class="mx-auto max-w-2xl space-y-4 p-6">
    <template v-if="loaded && entry">
      <h1 class="text-xl font-bold">{{ title }}</h1>
      <SafeMarkdown :text="entry.body" />
    </template>
    <div v-else-if="loaded">
      Wiki page not found
    </div>
  </div>
</template>
