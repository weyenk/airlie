<script setup lang="ts">
import { applyUpdate, updateReady } from "@/lib/sw";
</script>

<template>
  <div
    v-if="updateReady"
    role="status"
    class="fixed bottom-4 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3 rounded-lg border border-border bg-card px-4 py-2.5 text-[13px] shadow-lg"
    style="margin-bottom: env(safe-area-inset-bottom)"
  >
    <span class="font-medium">Update ready</span>
    <button type="button" class="rounded-md bg-primary px-3 py-1 font-semibold text-primary-foreground" @click="applyUpdate()">
      Reload
    </button>
  </div>
</template>
