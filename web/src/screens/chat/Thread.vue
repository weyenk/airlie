<script setup lang="ts">
import { computed, watch } from "vue";
import type { ThreadRow } from "@/api/sessions";
import type { ChatItem } from "@/stores/chat";
import { useTurns } from "@/stores/turns";
import Bubble from "./Bubble.vue";
import MessageView from "./thread/MessageView.vue";

const WELCOME =
  "Hi, I'm airlie's assistant. Ask me how airlie works or what's going on. Type / for commands such as /story or /clear.";
/** The thread of a chat session (`session`: its messages, one `MessageView` each), or today's items (`items`). */
const props = defineProps<{ items?: ChatItem[]; session?: ThreadRow }>();
const turns = useTurns();
// the session's Chat is made once its row is known; the computeds only read it
watch(() => props.session, (s) => s && turns.chatFor(s), { immediate: true });
const messages = computed(() => (props.session ? turns.messagesOf(props.session.id) : []));
const streaming = computed(() => !!props.session && ["submitted", "streaming"].includes(turns.status(props.session.id)));
const empty = computed(() => (props.session ? !messages.value.length : !props.items?.length));
const emit = defineEmits<{ quick: [text: string] }>();

const QUICK = ["What can you do?", "What's running right now?", "/story"];
</script>

<template>
  <div role="log" aria-label="Conversation" :aria-busy="streaming" class="mx-auto flex max-w-[780px] flex-col gap-4 px-5 pt-3.5 pb-6 max-[640px]:px-3">
    <div v-if="empty">
      <div data-msg="chat" class="flex items-start gap-2.5">
        <span aria-hidden="true" class="inline-flex size-7 flex-none items-center justify-center rounded-lg bg-primary text-sm font-extrabold text-primary-foreground">a</span>
        <div data-welcome class="max-w-[620px] rounded-[14px] rounded-tl-[4px] border border-border bg-card px-3.5 py-2.5 leading-normal">{{ WELCOME }}</div>
      </div>
      <div class="mt-2 ml-[38px] flex flex-wrap gap-1.5 max-[640px]:ml-0">
        <button
          v-for="q in QUICK"
          :key="q"
          type="button"
          data-quick
          class="rounded-full border border-border bg-card px-3 py-[5px] text-[13px] hover:bg-muted"
          @click="emit('quick', q)"
        >
          {{ q }}
        </button>
      </div>
    </div>
    <template v-if="session">
      <MessageView v-for="(m, i) in messages" :key="m.id" :message="m" :session-id="session.id" :last="i === messages.length - 1" />
    </template>
    <template v-else>
      <Bubble v-for="(item, i) in items" :key="i" :item="item" />
    </template>
  </div>
</template>
