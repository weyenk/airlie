<script setup lang="ts">
import { watch } from "vue";
import { useRoute } from "vue-router";
import { useMediaQuery } from "@vueuse/core";
import ChatPanel from "./ChatPanel.vue";
import PanelSlideOver from "./PanelSlideOver.vue";
import type { ThreadRow } from "@/api/sessions";
import ChatHeader from "@/screens/chat/ChatHeader.vue";
import { PHONE_QUERY } from "@/lib/breakpoints";
import { useChatPanel } from "@/stores/chatPanel";
import { LEGACY, rememberChat, useSessions } from "@/stores/sessions";

/** The chat screen's frame under the top bar: the panel at the left on a desktop, a slide-over on a phone, and the
 * chat row (`ChatHeader`: its button shows or hides the panel) above the thread in the slot. `session` is the open chat
 * (null for none, or the legacy thread); `title` names what is open when there is no session. */
defineProps<{ session: ThreadRow | null; title?: string }>();
const route = useRoute();
const sessions = useSessions();
const panel = useChatPanel();
const phone = useMediaQuery(PHONE_QUERY);

watch(
  () => route.params.session,
  (id) => {
    sessions.currentId = typeof id === "string" && id ? id : null;
    rememberChat(sessions.currentId === LEGACY ? null : sessions.currentId);
  },
  { immediate: true },
);
</script>

<template>
  <div class="flex h-[calc(100dvh-var(--header-h))] max-[640px]:h-[calc(100dvh-var(--header-h)-var(--tabbar-h))]">
    <div v-if="!phone && panel.desktopOpen" class="w-[272px] shrink-0 border-r border-border">
      <ChatPanel :current-id="sessions.currentId" @close="panel.toggleDesktop()" />
    </div>
    <div class="flex min-w-0 grow flex-col">
      <ChatHeader :session="session" :title="title" />
      <div class="flex min-h-0 grow flex-col"><slot /></div>
    </div>
    <PanelSlideOver v-if="phone" v-model:open="panel.phoneOpen" />
  </div>
</template>
