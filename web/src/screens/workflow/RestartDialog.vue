<script setup lang="ts">
import { ref, watch } from "vue";
import { restartStory } from "@/api/workflows";
import type { Restarted } from "@/api/workflows";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { restartKeeps } from "./restart";

/** The confirm for "Restart from here": what is kept, what goes, and that the story moves onto the latest main. The
 * server's refusal (running, a merged PR, a kept layer that does not replay) is shown as it says it. */
const props = defineProps<{ storyKey: string; ask: { point: string; label: string } | null }>();
const emit = defineEmits<{ close: []; done: [result: Restarted] }>();

const busy = ref(false);
const error = ref<string>();
watch(
  () => props.ask,
  () => {
    error.value = undefined;
  },
);

async function go() {
  if (!props.ask) return;
  busy.value = true;
  error.value = undefined;
  try {
    emit("done", await restartStory(props.storyKey, props.ask.point));
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <Dialog :open="!!ask" @update:open="(v: boolean) => !v && !busy && emit('close')">
    <DialogContent data-restart-dialog :show-close-button="false">
      <DialogHeader>
        <DialogTitle>Restart {{ storyKey }} from {{ ask?.label }}?</DialogTitle>
        <DialogDescription>It moves onto the latest main and goes back to Approved; the line picks it up on its next beat.</DialogDescription>
      </DialogHeader>
      <dl class="grid gap-2 text-sm">
        <div>
          <dt class="font-semibold">Kept</dt>
          <dd data-kept class="text-muted-foreground">{{ ask ? restartKeeps(ask.point) : "" }}</dd>
        </div>
        <div>
          <dt class="font-semibold">Discarded</dt>
          <dd data-discarded class="text-muted-foreground">
            Its open pull requests are closed and their branches deleted, the branches of this step and every step after it
            are deleted, and the run so far is archived on disk.
          </dd>
        </div>
      </dl>
      <p v-if="error" role="alert" data-restart-error class="break-words rounded-lg bg-danger px-3 py-2 text-xs font-medium text-danger-foreground">{{ error }}</p>
      <DialogFooter>
        <Button variant="outline" data-restart-cancel :disabled="busy" @click="emit('close')">Cancel</Button>
        <Button variant="destructive" data-restart-ok :disabled="busy" @click="go">Restart from here</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
