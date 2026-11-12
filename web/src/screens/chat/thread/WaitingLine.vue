<script setup lang="ts">
import { ref } from "vue";
import { RouterLink } from "vue-router";
import type { EngineStatus } from "@/api/engines";
import EngineStatusLine from "@/components/EngineStatusLine.vue";
import { useTurns } from "@/stores/turns";

/** The engine a turn waits for, said inline under that turn, with what the person can do about it. */
const props = defineProps<{ status: EngineStatus; sessionId: string }>();
const turns = useTurns();
const closed = ref(false);

const failed = (): boolean => props.status.state === "failed";
</script>

<template>
  <div v-if="!closed" data-waiting class="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-lg border border-border bg-card px-3 py-2">
    <EngineStatusLine :status="status" />
    <div class="flex gap-2 text-xs font-semibold">
      <template v-if="failed()">
        <button type="button" class="rounded-md border border-border px-2.5 py-1 hover:bg-row-hover" @click="turns.retry(sessionId)">Retry</button>
        <RouterLink to="/admin" class="rounded-md border border-border px-2.5 py-1 hover:bg-row-hover">Open log</RouterLink>
      </template>
      <template v-else>
        <button v-if="status.state === 'waiting'" type="button" class="rounded-md border border-border px-2.5 py-1 hover:bg-row-hover" @click="closed = true">Wait</button>
        <button type="button" class="rounded-md border border-border px-2.5 py-1 hover:bg-row-hover" @click="turns.stop(sessionId)">Cancel</button>
      </template>
    </div>
  </div>
</template>
