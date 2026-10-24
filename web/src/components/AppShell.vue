<script setup lang="ts">
import TabBar from "./TabBar.vue";
import TopBar from "./TopBar.vue";
</script>

<template>
  <TopBar />
  <main class="pb-16 min-[641px]:pb-0">
    <slot />
  </main>
  <TabBar />
</template>
