<script setup lang="ts">
import { ref, watch } from "vue";
import { ApiError } from "@/api";
import { adoptWorkflow } from "@/api/workflow_defs";
import type { Adopted } from "@/api/workflow_defs";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

/** The confirm for "Use the current workflow", shared by the story screen and the card. The server's refusal (409: the
 * story is running; 422: the card cannot adopt it) is shown as it says it; any other failure is the generic banner. */
const props = defineProps<{ open: boolean; storyKey: string; running: boolean }>();
const emit = defineEmits<{ "update:open": [open: boolean]; adopted: [result: Adopted] }>();

const busy = ref(false);
const refusal = ref<string>();
const failure = ref<string>();
watch(
  () => props.open,
  () => {
    refusal.value = undefined;
    failure.value = undefined;
  },
);

async function go() {
  busy.value = true;
  refusal.value = undefined;
  failure.value = undefined;
  try {
    const result = await adoptWorkflow(props.storyKey);
    emit("update:open", false);
    emit("adopted", result);
  } catch (e) {
    if (e instanceof ApiError && (e.status === 409 || e.status === 422)) refusal.value = e.message;
    else failure.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v: boolean) => !busy && emit('update:open', v)">
    <DialogContent data-adopt-dialog :show-close-button="false">
      <DialogHeader>
        <DialogTitle>Use the current workflow?</DialogTitle>
        <DialogDescription>Changed nodes and everything after them run again. Unchanged work is kept.</DialogDescription>
      </DialogHeader>
      <p v-if="refusal" role="alert" data-adopt-error class="break-words rounded-lg bg-danger px-3 py-2 text-xs font-medium text-danger-foreground">{{ refusal }}</p>
      <ErrorBanner v-if="failure" :error="failure" />
      <DialogFooter>
        <Button variant="outline" data-adopt-cancel :disabled="busy" @click="emit('update:open', false)">Keep</Button>
        <Button data-adopt-ok :disabled="busy || running" @click="go">Use it</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
