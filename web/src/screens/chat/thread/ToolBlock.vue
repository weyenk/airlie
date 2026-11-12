<script setup lang="ts">
import { useMediaQuery } from "@vueuse/core";
import type { ToolUIPart } from "ai";
import { ChevronRight } from "lucide-vue-next";
import { DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from "reka-ui";
import { computed, ref } from "vue";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { PHONE_QUERY } from "@/lib/breakpoints";
import DiffBlock from "./DiffBlock.vue";
import ToolDetail from "./ToolDetail.vue";
import { diffOf, inputOf, linesOf, outputText, PHASE_TEXT, phaseOf, summaryOf, textOfValue, toolName } from "./tool";

/**
 * One tool call as a collapsed row: its name, a line saying what it touched and its state. A click opens its input and
 * output; hovering the row on a desktop shows the call in full in a card, and tapping it on a phone opens that as a
 * bottom sheet (rich detail stays out of the plain layout).
 */
const props = defineProps<{ part: ToolUIPart; interrupted: boolean }>();

const SHOWN = 12;
const phone = useMediaQuery(PHONE_QUERY);
const open = ref(false);
const card = ref(false);
const sheet = ref(false);
const all = ref(false);

const phase = computed(() => phaseOf(props.part, props.interrupted));
const diff = computed(() => diffOf(props.part));
const entries = computed(() => (diff.value ? [] : Object.entries(inputOf(props.part))));
const output = computed(() => outputText(props.part));
const outLines = computed(() => linesOf(output.value));
const long = computed(() => outLines.value.length > SHOWN);
const shown = computed(() => (long.value && !all.value ? outLines.value.slice(0, SHOWN).join("\n") : output.value));
const counts = computed(() => {
  const d = diff.value;
  return d ? { added: linesOf(d.new).length, removed: linesOf(d.old ?? "").length } : null;
});

function press(): void {
  card.value = false;
  if (phone.value) sheet.value = true;
  else open.value = !open.value;
}

function hover(next: boolean): void {
  card.value = next && !phone.value && !open.value;
}
</script>

<template>
  <div data-tool :data-phase="phase" class="overflow-hidden rounded-lg border border-border bg-card text-[13px]">
    <HoverCard :open="card" :open-delay="250" :close-delay="100" @update:open="hover">
      <HoverCardTrigger as-child>
        <button
          type="button"
          :aria-expanded="phone ? undefined : open"
          :aria-haspopup="phone ? 'dialog' : undefined"
          class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-row-hover"
          @click="press"
        >
          <ChevronRight v-if="!phone" aria-hidden="true" :class="['size-3.5 flex-none text-muted-foreground transition-transform', open && 'rotate-90']" />
          <span class="flex-none font-mono font-semibold">{{ toolName(part) }}</span>
          <span class="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground">{{ summaryOf(part) }}</span>
          <span v-if="counts" class="flex flex-none gap-1.5 text-xs font-semibold"><span class="text-ok-foreground">+{{ counts.added }}</span><span class="text-danger-foreground">-{{ counts.removed }}</span></span>
          <span :class="['flex-none text-xs font-medium', phase === 'failed' ? 'text-danger-foreground' : phase === 'interrupted' || phase === 'denied' ? 'text-warn-foreground' : 'text-muted-foreground']">{{ PHASE_TEXT[phase] }}</span>
        </button>
      </HoverCardTrigger>
      <HoverCardContent side="bottom" align="start"><ToolDetail :part="part" :interrupted="interrupted" /></HoverCardContent>
    </HoverCard>
    <div v-if="open && !phone" class="flex flex-col gap-2 border-t border-border px-3 py-2">
      <DiffBlock v-if="diff" :path="diff.path" :old="diff.old" :new="diff.new" />
      <dl v-if="entries.length" class="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <template v-for="[key, value] in entries" :key="key">
          <dt class="text-muted-foreground">{{ key }}</dt>
          <dd class="m-0 font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere]">{{ textOfValue(value) }}</dd>
        </template>
      </dl>
      <div v-if="output">
        <pre data-output :class="['m-0 max-h-80 overflow-y-auto rounded-md border bg-code p-2 font-mono text-xs leading-normal whitespace-pre-wrap [overflow-wrap:anywhere]', phase === 'failed' ? 'border-danger-foreground/40' : 'border-border']">{{ shown }}</pre>
        <button v-if="long && !all" type="button" data-more class="mt-1 text-xs font-medium text-primary hover:underline" @click="all = true">Show all {{ outLines.length }} lines</button>
      </div>
      <p v-else-if="phase === 'running'" class="m-0 text-muted-foreground">Running…</p>
    </div>
    <DialogRoot v-if="phone" v-model:open="sheet">
      <DialogPortal>
        <DialogOverlay class="fixed inset-0 z-50 bg-background/80" />
        <DialogContent
          class="fixed inset-x-0 bottom-0 z-50 max-h-[80vh] overflow-y-auto rounded-t-xl border border-border bg-card p-4 pb-[calc(1rem+env(safe-area-inset-bottom,0px))] shadow-modal outline-none"
        >
          <DialogTitle class="sr-only">{{ toolName(part) }} call</DialogTitle>
          <DialogDescription class="sr-only">The call's input, output and state</DialogDescription>
          <ToolDetail :part="part" :interrupted="interrupted" />
        </DialogContent>
      </DialogPortal>
    </DialogRoot>
  </div>
</template>
