<script setup lang="ts">
import { useTurns } from "@/stores/turns";

/** A reply that was cut: the server may still run the turn (reconnect or stop it), or it is over and the reply is thin (retry). */
defineProps<{ sessionId: string; running: boolean }>();
const turns = useTurns();
</script>

<template>
  <div data-cut role="status" class="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-lg border border-dashed border-border px-3 py-2 text-[13px]">
    <span class="text-muted-foreground">{{ running ? "Stream cut. The turn kept running on the server and is saved" : "Cut off" }}</span>
    <div class="flex gap-2 text-xs font-semibold">
      <template v-if="running">
        <button type="button" class="rounded-md border border-border px-2.5 py-1 hover:bg-row-hover" @click="turns.reconnect(sessionId)">Reconnect</button>
        <button type="button" class="rounded-md border border-border px-2.5 py-1 hover:bg-row-hover" @click="turns.stop(sessionId)">Stop turn</button>
      </template>
      <button v-else type="button" class="rounded-md border border-border px-2.5 py-1 hover:bg-row-hover" @click="turns.retry(sessionId)">Retry</button>
    </div>
  </div>
</template>
