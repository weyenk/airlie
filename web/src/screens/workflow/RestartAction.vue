<script setup lang="ts">
import { RotateCcw } from "lucide-vue-next";
import { computed, inject } from "vue";
import { Button } from "@/components/ui/button";
import { RESTART } from "./restart";

/** "Restart from here" for the step `nodeId`, under its card: nothing for a step the story has not reached or that has
 * no restart, and disabled while the story runs. */
const props = defineProps<{ nodeId: string }>();
const restart = inject(RESTART, null);
const point = computed(() => restart?.pointOf(props.nodeId) ?? null);
</script>

<template>
  <div v-if="restart && point" data-restart class="flex items-center gap-2">
    <Button
      type="button"
      variant="destructive"
      size="xs"
      data-restart-button
      :data-point="point"
      :disabled="restart.running.value"
      @click="restart.ask(nodeId)"
    >
      <RotateCcw class="size-3.5" />Restart from here
    </Button>
    <span v-if="restart.running.value" class="text-xs text-muted-foreground">stop it first</span>
  </div>
</template>
