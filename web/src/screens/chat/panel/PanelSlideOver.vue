<script setup lang="ts">
import { DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from "reka-ui";
import ChatPanel from "./ChatPanel.vue";
import { useSessions } from "@/stores/sessions";

/** The phone's chat panel: a left sheet between the top bar and the tab bar; the scrim, Escape and a pick close it. */
const open = defineModel<boolean>("open", { default: false });
const sessions = useSessions();
</script>

<template>
  <DialogRoot v-model:open="open">
    <DialogPortal>
      <DialogOverlay
        class="fixed inset-x-0 top-(--header-h) bottom-(--tabbar-h) z-[4] bg-background/80 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0"
      />
      <DialogContent
        data-test="panel-slide-over"
        class="fixed top-(--header-h) bottom-(--tabbar-h) left-0 z-[5] w-[min(320px,88vw)] border-r border-border bg-background shadow-lg outline-none data-[state=closed]:animate-out data-[state=closed]:slide-out-to-left data-[state=open]:animate-in data-[state=open]:slide-in-from-left"
      >
        <DialogTitle class="sr-only">Chats</DialogTitle>
        <DialogDescription class="sr-only">Your chats, by repository</DialogDescription>
        <ChatPanel :current-id="sessions.currentId" @pick="open = false" @close="open = false" />
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
