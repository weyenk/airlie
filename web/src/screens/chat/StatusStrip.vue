<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ApiError, request } from "@/api";
import { listStories } from "@/api/workflows";
import { liveResource } from "@/stores/live";
import { useFeedStore } from "@/stores/feed";
import { useWorkflowsStore } from "@/stores/workflows";

const POLL_MS = 15000;
const workflows = useWorkflowsStore();
const feed = useFeedStore();

// how many stories are blocked: a page of one is enough, the answer is its total
const blocked = liveResource("stories", () => listStories({ blocked: true, size: 1 }));
const needs = computed(() => blocked.data.value?.total ?? 0);
const running = computed(() => workflows.running.length);

// Studio is online while the engine answers /v1/models; nothing in the feed says when it stops, so ask again
const studio = ref<"online" | "offline" | null>(null);
let asked = 0;
async function checkStudio(): Promise<void> {
  const mine = ++asked;
  try {
    await request("/v1/models");
    if (mine === asked) studio.value = "online"; // a newer question has been asked: its answer wins
  } catch (e) {
    if (mine !== asked) return;
    if (e instanceof ApiError && e.status === 401) return; // signing in again is the page's job
    studio.value = "offline";
  }
}
void checkStudio();
const timer = setInterval(() => void checkStudio(), POLL_MS);
watch(() => feed.generation, () => void checkStudio());
onBeforeUnmount(() => clearInterval(timer));

const pill = "inline-flex items-center gap-1.5 rounded-full px-2.5 py-[3px] text-xs font-semibold";
</script>

<template>
  <div data-test="status-strip" class="flex flex-wrap gap-1.5">
    <RouterLink v-if="needs" to="/workflows?blocked=true" data-strip="needs" :class="[pill, 'bg-warn text-warn-foreground']">
      {{ needs }} {{ needs === 1 ? "needs" : "need" }} you
    </RouterLink>
    <RouterLink to="/workflows" data-strip="running" :class="[pill, running ? 'bg-info text-info-foreground' : 'bg-chip text-chip-foreground']">
      <span v-if="running" class="size-[7px] rounded-full bg-current" />{{ running }} running
    </RouterLink>
    <span v-if="studio" data-strip="studio" :class="[pill, studio === 'online' ? 'bg-ok text-ok-foreground' : 'bg-danger text-danger-foreground']">
      <span class="size-[7px] rounded-full bg-current" />Studio {{ studio }}
    </span>
  </div>
</template>
