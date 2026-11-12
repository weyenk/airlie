<script setup lang="ts">
import { useMediaQuery } from "@vueuse/core";
import { Cpu, GitBranch, PanelLeft } from "lucide-vue-next";
import { computed } from "vue";
import type { ThreadRow } from "@/api/sessions";
import { Button } from "@/components/ui/button";
import { PHONE_QUERY } from "@/lib/breakpoints";
import { useChatPanel } from "@/stores/chatPanel";
import { useEngines } from "@/stores/engines";
import { useTurns } from "@/stores/turns";
import ContextMeter from "./ContextMeter.vue";
import SessionStatus from "./SessionStatus.vue";

/**
 * The chat row above the thread: the button that shows the panel, the title and, for a repository chat, its repository
 * and branch; the model, the context readout and the status chip. On a phone it keeps the button, the title and the chip
 * (the rest is in the chip's sheet).
 */
const props = defineProps<{ session: ThreadRow | null }>();
const panel = useChatPanel();
const engines = useEngines();
const turns = useTurns();
const phone = useMediaQuery(PHONE_QUERY);

const shown = computed(() => (phone.value ? panel.phoneOpen : panel.desktopOpen));
const title = computed(() => props.session?.title || "New chat");
const model = computed(() => engines.forRole("chat")?.name ?? null);
const usage = computed(() => (props.session ? turns.usage(props.session.id) : null));
const chip = "inline-flex min-w-0 items-center gap-1 rounded-md bg-chip px-1.5 py-0.5 text-xs text-chip-foreground";
</script>

<template>
  <header data-test="chat-header" class="flex items-center gap-2 border-b border-border px-3 py-1.5">
    <Button variant="ghost" size="icon-sm" aria-label="Chats" :aria-expanded="shown" @click="phone ? panel.togglePhone() : panel.toggleDesktop()"><PanelLeft /></Button>
    <h1 class="m-0 min-w-0 grow truncate text-sm font-semibold">{{ title }}</h1>
    <template v-if="session && !phone">
      <span v-if="session.repo" data-repo :class="chip">{{ session.repo }}</span>
      <span v-if="session.branch" data-branch :class="[chip, 'font-mono']"><GitBranch aria-hidden="true" class="size-3 flex-none" />{{ session.branch }}</span>
      <span v-if="model" data-model class="inline-flex items-center gap-1 text-xs text-muted-foreground"><Cpu aria-hidden="true" class="size-3 flex-none" />{{ model }}</span>
      <ContextMeter :used="usage?.used ?? null" :limit="usage?.limit ?? null" />
    </template>
    <SessionStatus v-if="session" :session="session" />
  </header>
</template>
