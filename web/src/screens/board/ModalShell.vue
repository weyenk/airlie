<script setup lang="ts">
import { X } from "lucide-vue-next";
import { DialogClose, DialogContent, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from "reka-ui";

defineOptions({ name: "AirlieModalShell" });

// The card and Create modal's frame: a dimmed backdrop that scrolls, and a card 760px wide at the top of it;
// below 1024px the card is the whole screen. reka-ui's dialog traps focus, closes on Escape and on a click on
// the backdrop (both arrive as `close`), and hands focus back when it goes.
defineProps<{ title: string; titleClass?: string }>();
const emit = defineEmits<{ close: []; "close-auto-focus": [event: Event]; "open-auto-focus": [event: Event] }>();
</script>

<template>
  <DialogRoot :open="true" @update:open="(open: boolean) => !open && emit('close')">
    <DialogPortal>
      <DialogOverlay data-overlay class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-overlay px-4 py-10 max-[1023px]:p-0">
        <DialogContent
          aria-modal="true"
          :aria-describedby="undefined"
          class="relative w-[760px] max-w-full rounded-[14px] border bg-card text-card-foreground shadow-modal outline-none max-[1023px]:fixed max-[1023px]:inset-0 max-[1023px]:w-full max-[1023px]:max-w-none max-[1023px]:overflow-y-auto max-[1023px]:rounded-none max-[1023px]:border-0"
          @pointer-down-outside="
            (e: CustomEvent<{ originalEvent: PointerEvent }>) => {
              const o = e.detail.originalEvent;
              const t = o.target as HTMLElement;
              // a click on the backdrop's own scrollbar is not a click on the backdrop
              if (o.offsetX > t.clientWidth || o.offsetY > t.clientHeight) e.preventDefault();
            }
          "
          @open-auto-focus="(e: Event) => emit('open-auto-focus', e)"
          @close-auto-focus="(e: Event) => emit('close-auto-focus', e)"
        >
          <div class="flex items-center gap-2.5 px-5 pt-4">
            <DialogTitle as="span" :class="titleClass ?? 'font-mono text-xs font-normal text-muted-foreground'">{{ title }}</DialogTitle>
            <span class="grow" />
            <DialogClose
              aria-label="Close"
              class="inline-flex size-8 items-center justify-center rounded-lg border text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X class="size-4" />
            </DialogClose>
          </div>
          <div class="flex flex-col gap-4 px-5 pt-2 pb-5">
            <slot />
          </div>
        </DialogContent>
      </DialogOverlay>
    </DialogPortal>
  </DialogRoot>
</template>
