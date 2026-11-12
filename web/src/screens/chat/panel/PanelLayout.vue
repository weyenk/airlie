<script setup lang="ts">
import { computed, watch } from "vue";
import { useRoute } from "vue-router";
import { useMediaQuery } from "@vueuse/core";
import { PanelLeft } from "lucide-vue-next";
import ChatPanel from "./ChatPanel.vue";
import PanelSlideOver from "./PanelSlideOver.vue";
import { Button } from "@/components/ui/button";
import { PHONE_QUERY, useChatPanel } from "@/stores/chatPanel";
import { rememberChat, useSessions } from "@/stores/sessions";

/** The chat screen's frame under the top bar: the panel at the left on a desktop, a slide-over on a phone, and the
 * chat row (its button shows or hides the panel) above the thread in the slot. */
const route = useRoute();
const sessions = useSessions();
const panel = useChatPanel();
const phone = useMediaQuery(PHONE_QUERY);
const shown = computed(() => (phone.value ? panel.phoneOpen : panel.desktopOpen));

watch(
  () => route.params.session,
  (id) => {
    sessions.currentId = typeof id === "string" && id ? id : null;
    rememberChat(sessions.currentId);
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
      <div class="flex items-center border-b border-border px-3 py-1.5">
        <Button variant="ghost" size="icon-sm" :aria-label="shown ? 'Hide chats' : 'Show chats'" :aria-expanded="shown" @click="phone ? panel.togglePhone() : panel.toggleDesktop()"><PanelLeft /></Button>
      </div>
      <div class="min-h-0 grow"><slot /></div>
    </div>
    <PanelSlideOver v-if="phone" v-model:open="panel.phoneOpen" />
  </div>
</template>
