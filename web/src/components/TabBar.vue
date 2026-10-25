<script setup lang="ts">
import { useRoute } from "vue-router";
import { TABS } from "./tabs";

const route = useRoute();
</script>

<template>
  <nav
    data-test="tabbar"
    aria-label="Main"
    class="fixed inset-x-0 bottom-0 z-[3] hidden h-16 items-center justify-around border-t border-border bg-bar pb-1.5 max-[640px]:flex"
  >
    <RouterLink
      v-for="t in TABS"
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
