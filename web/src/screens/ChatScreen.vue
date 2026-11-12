<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import EmptyState from "@/components/EmptyState.vue";
import ErrorBanner from "@/components/ErrorBanner.vue";
import { Button } from "@/components/ui/button";
import { useChatStore } from "@/stores/chat";
import { LEGACY, rememberChat, useSessions } from "@/stores/sessions";
import { useTurns } from "@/stores/turns";
import Composer from "./chat/Composer.vue";
import PanelLayout from "./chat/panel/PanelLayout.vue";
import Thread from "./chat/Thread.vue";

/**
 * The chat screen at `/` and `/chat/:session`: the chat panel and the chat row (PanelLayout), the open chat's thread and its
 * composer. `/` opens the chat this browser had open last (the router), else the newest, else a fresh plain chat. The
 * thread an earlier version of the app kept in this tab is shown as `/chat/legacy`, read-only.
 */
const route = useRoute();
const router = useRouter();
const sessions = useSessions();
const chat = useChatStore();
const turns = useTurns();
const composer = ref<InstanceType<typeof Composer> | null>(null);
const scroller = ref<HTMLElement | null>(null);
const failed = ref<Error | undefined>();
const problem = computed(() => failed.value ?? sessions.error);

const id = computed(() => (typeof route.params.session === "string" ? route.params.session : ""));
const legacy = computed(() => id.value === LEGACY);
const row = computed(() => (id.value ? (sessions.rows.find((r) => r.id === id.value) ?? null) : null));
const gone = computed(() => !!id.value && !legacy.value && sessions.loaded && !row.value);
// the open chat's Chat is made here, once its row is known; the computeds below only read it
watch(row, (r) => r && turns.chatFor(r), { immediate: true });
const messages = computed(() => (row.value ? turns.messagesOf(row.value.id) : []));

// `/` opened the chat this browser remembered, and it is gone (deleted on another device): forget it and open the
// newest instead of saying it no longer exists
watch(
  gone,
  (isGone) => {
    if (!isGone || route.redirectedFrom?.path !== "/") return;
    rememberChat(null);
    void router.replace("/");
  },
  { immediate: true },
);

async function open(): Promise<void> {
  if (id.value || !sessions.loaded || failed.value) return;
  try {
    const target = sessions.rows.length ? [...sessions.rows].sort((a, b) => b.updated - a.updated)[0] : await sessions.create("plain");
    await router.replace(`/chat/${encodeURIComponent(target.id)}`);
  } catch (e) {
    failed.value = e instanceof Error ? e : new Error(String(e));
  }
}
watch([id, () => sessions.loaded], () => void open(), { immediate: true });

async function startNew(): Promise<void> {
  failed.value = undefined;
  try {
    await router.push(`/chat/${encodeURIComponent((await sessions.create("plain")).id)}`);
  } catch (e) {
    failed.value = e instanceof Error ? e : new Error(String(e));
  }
}

function removeLegacy(): void {
  chat.clearLegacy();
  void router.replace("/");
}

const NEAR = 80;
function scrollDown(): void {
  const el = scroller.value;
  if (el) el.scrollTop = el.scrollHeight;
}
// A new word follows the end of the thread only when the person was already reading the end (within NEAR px of
// it before the update); their own message always does. `flush: "pre"` reads the position before the DOM grows.
const mine = computed(() => messages.value.filter((m) => m.role === "user").length);
const size = computed(() => {
  const parts = messages.value.at(-1)?.parts ?? [];
  return parts.reduce((n, p) => n + 1 + (typeof (p as { text?: unknown }).text === "string" ? (p as { text: string }).text.length : 0), 0);
});
let sentThen = mine.value;
watch(
  [id, () => messages.value.length, size, mine],
  ([now], [before]) => {
    const el = scroller.value;
    const own = mine.value !== sentThen || now !== before;
    sentThen = mine.value;
    const near = !el || el.scrollHeight - el.scrollTop - el.clientHeight < NEAR;
    if (own || near) void nextTick(scrollDown);
  },
  { flush: "pre" },
);

function quick(text: string): void {
  if (!row.value) return;
  if (text.startsWith("/")) {
    chat.setDraft(`${text} `); // a command needs its request
    void nextTick(() => composer.value?.focus());
    return;
  }
  void turns.send(row.value.id, { text });
}

onMounted(scrollDown);
</script>

<template>
  <PanelLayout :session="row" :title="legacy ? 'Legacy chat' : undefined">
    <div v-if="legacy" class="flex min-h-0 flex-1 flex-col">
      <div ref="scroller" data-test="thread-scroll" class="min-h-0 flex-1 overflow-x-hidden overflow-y-auto">
        <Thread :items="chat.thread" />
      </div>
      <div data-test="legacy-note" class="flex items-center gap-2 border-t border-border px-5 py-2.5 text-[13px] text-muted-foreground max-[640px]:px-3">
        <span class="grow">An earlier chat from this tab, kept as it was. It is read-only.</span>
        <Button size="sm" variant="outline" @click="removeLegacy">Remove it</Button>
      </div>
    </div>
    <div v-else-if="row" class="flex min-h-0 flex-1 flex-col">
      <div ref="scroller" data-test="thread-scroll" class="min-h-0 flex-1 overflow-x-hidden overflow-y-auto">
        <Thread :session="row" @quick="quick" />
      </div>
      <Composer ref="composer" :session="row" />
    </div>
    <div v-else class="flex flex-col items-center gap-3 px-4 py-8">
      <ErrorBanner v-if="problem" :error="problem" />
      <EmptyState v-if="gone" text="This chat no longer exists." />
      <Button v-if="gone || problem" variant="outline" @click="startNew">Start a new chat</Button>
      <p v-else-if="!problem" role="status" class="m-0 text-sm text-muted-foreground">Opening a chat</p>
    </div>
  </PanelLayout>
</template>
