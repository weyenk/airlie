<script setup lang="ts">
import { computed } from "vue";
import { useFeedStore } from "@/stores/feed";

const feed = useFeedStore();
// "connecting" is the first moment of a page: reconnecting is the honest word for anything not yet live
const live = computed(() => feed.state === "live");
</script>

<template>
  <span
    data-test="live-badge"
    :class="[
      'inline-flex items-center gap-[5px] whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-bold',
      live ? 'bg-ok text-ok-foreground' : 'bg-warn text-warn-foreground',
    ]"
  >
    <span class="size-[7px] rounded-full bg-current" />{{ live ? "Live" : "Reconnecting" }}
  </span>
</template>
