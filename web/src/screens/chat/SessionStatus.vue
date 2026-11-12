<script setup lang="ts">
import { useMediaQuery } from "@vueuse/core";
import { ChevronDown } from "lucide-vue-next";
import {
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
  DialogTrigger,
  PopoverContent,
  PopoverPortal,
  PopoverRoot,
  PopoverTrigger,
} from "reka-ui";
import { computed, ref } from "vue";
import type { EngineStatus } from "@/api/engines";
import type { ThreadRow } from "@/api/sessions";
import EngineStatusLine from "@/components/EngineStatusLine.vue";
import { PHONE_QUERY } from "@/lib/breakpoints";
import { useEngines } from "@/stores/engines";
import SessionStatusDetail from "./SessionStatusDetail.vue";

/**
 * The chat row's status chip: a dot that says nothing more while everything is ready, the engine line while the engine
 * or the chat's VM starts, downloads or waits, and the reason while one failed. A click opens what stands behind it, as a
 * popover on a desktop and a bottom sheet on a phone (where the chip stays a dot).
 */
const props = defineProps<{ session: ThreadRow }>();
const engines = useEngines();
const phone = useMediaQuery(PHONE_QUERY);
const open = ref(false);

const engine = computed(() => engines.forRole("chat"));
const vm = computed(() => (props.session.kind === "repository" ? engines.byName(`vm:${props.session.id}`) : undefined));
const MOVING = new Set<EngineStatus["state"]>(["downloading", "starting", "waiting", "stopping"]);

/** The one the chip says: a failure first, then whichever moves; the VM and the engine otherwise say nothing. */
const subject = computed(() => {
  const rows = [engine.value, vm.value].filter((r): r is EngineStatus => r !== undefined);
  const found = rows.find((r) => r.state === "failed") ?? rows.find((r) => MOVING.has(r.state));
  return found ? { status: found, vm: found === vm.value } : null;
});
const tone = computed(() => {
  if (!subject.value) return engine.value === undefined || engine.value.state === "stopped" ? "idle" : "ok";
  return subject.value.status.state === "failed" ? "danger" : "warn";
});
const TONE_CLASS = { ok: "text-ok-foreground", warn: "text-warn-foreground", danger: "text-danger-foreground", idle: "text-muted-foreground" };

const word = computed(() => {
  const s = subject.value?.status;
  if (!s) return tone.value === "idle" ? "idle" : "ready";
  return `${s.state} ${subject.value?.vm ? "VM" : s.name}`;
});
const label = computed(() => `a VM for ${props.session.repo}`);
const showsLine = computed(() => subject.value !== null && !phone.value);
</script>

<template>
  <component :is="phone ? DialogRoot : PopoverRoot" v-model:open="open">
    <component :is="phone ? DialogTrigger : PopoverTrigger" as-child>
      <button
        type="button"
        data-status-chip
        :data-tone="tone"
        :aria-label="showsLine ? undefined : `Status: ${word}`"
        :class="['flex items-center gap-1.5 rounded-lg border border-border px-2 py-1 text-xs font-semibold hover:bg-row-hover', TONE_CLASS[tone]]"
      >
        <span class="size-[7px] flex-none rounded-full bg-current" />
        <template v-if="showsLine && subject">
          <EngineStatusLine :status="subject.status" :label="subject.vm ? label : undefined" />
          <span v-if="subject.vm && subject.status.state === 'starting' && subject.status.detail" class="font-medium text-muted-foreground">{{ subject.status.detail }}</span>
        </template>
        <ChevronDown v-if="!phone" aria-hidden="true" class="size-3.5 text-muted-foreground" />
      </button>
    </component>
    <PopoverPortal v-if="!phone">
      <PopoverContent
        side="bottom"
        align="end"
        :side-offset="6"
        class="z-50 w-[min(330px,92vw)] rounded-xl border border-border bg-popover p-1.5 text-popover-foreground shadow-pop outline-none"
      >
        <SessionStatusDetail :session="session" />
      </PopoverContent>
    </PopoverPortal>
    <DialogPortal v-else>
      <DialogOverlay class="fixed inset-0 z-50 bg-background/80" />
      <DialogContent
        class="fixed inset-x-0 bottom-0 z-50 max-h-[80vh] overflow-y-auto rounded-t-xl border border-border bg-card p-3 pb-[calc(1rem+env(safe-area-inset-bottom,0px))] shadow-modal outline-none"
      >
        <DialogTitle class="sr-only">Chat status</DialogTitle>
        <DialogDescription class="sr-only">The chat's VM, engine, context, memory and branch</DialogDescription>
        <SessionStatusDetail :session="session" />
      </DialogContent>
    </DialogPortal>
  </component>
</template>
