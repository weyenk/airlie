<script setup lang="ts">
import type { FlowJob, JobDetail } from "@/api/workflows";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import FlowCardView from "./FlowCardView.vue";
import JobDetailView from "./JobDetail.vue";

/** A job's card and trace in a sheet from the bottom of the screen (the phone's stand-in for hover and the detail below). */
defineProps<{ open: boolean; job: FlowJob | null; detail: JobDetail | null; nodeLabel?: string }>();
const emit = defineEmits<{ close: [] }>();
</script>

<template>
  <Dialog :open="open" @update:open="(v: boolean) => !v && emit('close')">
    <DialogContent
      data-sheet
      class="top-auto bottom-0 left-0 max-h-[85vh] w-full max-w-full translate-x-0 translate-y-0 gap-3 overflow-y-auto rounded-t-2xl rounded-b-none border-x-0 border-b-0 p-3 pb-[calc(0.75rem+env(safe-area-inset-bottom,0px))] sm:max-w-full data-[state=open]:slide-in-from-bottom data-[state=closed]:slide-out-to-bottom data-[state=open]:zoom-in-100 data-[state=closed]:zoom-out-100 motion-reduce:animate-none"
    >
      <div data-grab class="mx-auto h-1 w-10 flex-none rounded-full bg-border" aria-hidden="true" />
      <DialogTitle class="sr-only">{{ nodeLabel ? `${nodeLabel} / ` : "" }}{{ job?.label ?? "Job" }}</DialogTitle>
      <DialogDescription class="sr-only">The step's card and its trace.</DialogDescription>
      <div v-if="job?.card" class="[&>[data-card]]:w-full [&>[data-card]]:max-w-full [&>[data-card]]:shadow-none">
        <FlowCardView :card="job.card" />
      </div>
      <JobDetailView v-if="detail" :detail="detail" :node-label="nodeLabel" />
      <section v-else aria-label="Job" class="rounded-xl border border-border bg-card p-[18px] text-muted-foreground">Loading…</section>
    </DialogContent>
  </Dialog>
</template>
