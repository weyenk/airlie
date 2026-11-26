<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { TABS } from "./tabs";
import { filterUpdateTab } from "@/lib/updateGating";
import { useSessionStore } from "@/stores/session";

const route = useRoute();
const session = useSessionStore();
// computed, not a one-shot const: main.ts mounts before the router guard learns who we are,
// so below 640px — where this bar is the only nav — a frozen list would drop Update forever.
const visibleTabs = computed(() => filterUpdateTab(TABS, session.me));
</script>

<template>
  <nav
    data-test="tabbar"
    aria-label="Main"
    class="fixed inset-x-0 bottom-0 z-[3] hidden h-(--tabbar-h) items-center justify-around border-t border-border bg-bar pb-[calc(0.375rem+env(safe-area-inset-bottom,0px))] max-[640px]:flex"
  >
    <RouterLink
      v-for="t in visibleTabs"
      :key="t.tab"
      :to="t.to"
      :aria-current="route.meta.tab === t.tab ? 'page' : undefined"
      :class="[
        'flex flex-col items-center gap-[3px] text-[11px] font-medium',
        route.meta.tab === t.tab ? 'text-primary' : 'text-muted-foreground',
      ]"
    >
      <component :is="t.icon" class="size-5" />{{ t.label }}
    </RouterLink>
  </nav>
</template>
