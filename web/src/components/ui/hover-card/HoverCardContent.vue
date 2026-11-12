<script setup lang="ts">
import type { HoverCardContentProps } from "reka-ui"
import type { HTMLAttributes } from "vue"
import { reactiveOmit } from "@vueuse/core"
import { cn } from "@/lib/cn"
import { HoverCardContent, HoverCardPortal, useForwardProps } from "reka-ui"

defineOptions({
  inheritAttrs: false,
})

const props = withDefaults(defineProps<HoverCardContentProps & { class?: HTMLAttributes["class"] }>(), {
  sideOffset: 6,
})

const delegatedProps = reactiveOmit(props, "class")
const forwarded = useForwardProps(delegatedProps)
</script>

<template>
  <HoverCardPortal>
    <HoverCardContent
      data-slot="hover-card-content"
      v-bind="{ ...forwarded, ...$attrs }"
      :class="cn('bg-popover text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 z-50 w-[min(440px,92vw)] rounded-lg border border-border p-3 shadow-pop outline-hidden', props.class)"
    >
      <slot />
    </HoverCardContent>
  </HoverCardPortal>
</template>
