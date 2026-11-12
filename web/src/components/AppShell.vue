<script setup lang="ts">
import ConfirmDialog from "./ConfirmDialog.vue";
import TabBar from "./TabBar.vue";
import TopBar from "./TopBar.vue";
</script>

<template>
  <TopBar />
  <main
    class="pr-[env(safe-area-inset-right)] pb-[calc(var(--tabbar-h)+env(safe-area-inset-bottom))] pl-[env(safe-area-inset-left)] min-[641px]:pb-0"
  >
    <slot />
  </main>
  <TabBar />
  <ConfirmDialog />
</template>
