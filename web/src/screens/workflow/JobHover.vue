<script setup lang="ts">
import { useMediaQuery } from "@vueuse/core";
import { HoverCardContent, HoverCardPortal, HoverCardRoot, HoverCardTrigger } from "reka-ui";
import { computed, inject } from "vue";
import type { FlowCard } from "@/api/workflows";
import { PHONE_QUERY } from "@/lib/breakpoints";
import FlowCardView from "./FlowCardView.vue";
import RestartAction from "./RestartAction.vue";
import { RESTART } from "./restart";

/** Wraps a box: on a laptop, hovering or focusing it opens its card, with "Restart from here" under it when the box's
 * step (`nodeId`) has one. A phone shows the card in the sheet instead. */
const props = defineProps<{ card: FlowCard | null | undefined; nodeId?: string }>();
const phone = useMediaQuery(PHONE_QUERY);
const restart = inject(RESTART, null);
const restartable = computed(() => !!props.nodeId && !!restart?.pointOf(props.nodeId));
</script>

<template>
  <slot v-if="(!card && !restartable) || phone" />
  <HoverCardRoot v-else :open-delay="250" :close-delay="100">
    <HoverCardTrigger as-child><slot /></HoverCardTrigger>
    <HoverCardPortal>
      <HoverCardContent side="bottom" :side-offset="6" :collision-padding="16" class="z-50 flex flex-col gap-1.5">
        <FlowCardView v-if="card" :card="card" />
        <div v-if="restartable && nodeId" class="w-fit rounded-[10px] border bg-card p-1.5 shadow-lg">
          <RestartAction :node-id="nodeId" />
        </div>
      </HoverCardContent>
    </HoverCardPortal>
  </HoverCardRoot>
</template>
