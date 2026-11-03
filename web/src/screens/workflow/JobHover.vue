<script setup lang="ts">
import { useMediaQuery } from "@vueuse/core";
import { HoverCardContent, HoverCardPortal, HoverCardRoot, HoverCardTrigger } from "reka-ui";
import type { FlowCard } from "@/api/workflows";
import { PHONE_QUERY } from "@/lib/breakpoints";
import FlowCardView from "./FlowCardView.vue";

/** Wraps a box: on a laptop, hovering or focusing it opens its card. A phone shows the card in the sheet instead. */
defineProps<{ card: FlowCard | null | undefined }>();
const phone = useMediaQuery(PHONE_QUERY);
</script>

<template>
  <slot v-if="!card || phone" />
  <HoverCardRoot v-else :open-delay="250" :close-delay="100">
    <HoverCardTrigger as-child><slot /></HoverCardTrigger>
    <HoverCardPortal>
      <HoverCardContent side="bottom" :side-offset="6" :collision-padding="16" class="z-50">
        <FlowCardView :card="card" />
      </HoverCardContent>
    </HoverCardPortal>
  </HoverCardRoot>
</template>
