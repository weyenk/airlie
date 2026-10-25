<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from "vue";
import { useChatStore } from "@/stores/chat";
import Composer from "./chat/Composer.vue";
import StatusStrip from "./chat/StatusStrip.vue";
import Thread from "./chat/Thread.vue";

const chat = useChatStore();
const composer = ref<InstanceType<typeof Composer> | null>(null);
const scroller = ref<HTMLElement | null>(null);

const NEAR = 80;
function scrollDown(): void {
  const el = scroller.value;
  if (el) el.scrollTop = el.scrollHeight;
}
// A new word follows the end of the thread only when the person was already reading the end (within NEAR px of
// it before the update); their own message always does. `flush: "pre"` reads the position before the DOM grows.
let sentThen = chat.sends;
watch(
  () => [chat.thread.length, chat.thread.at(-1)?.text?.length ?? 0, chat.thread.at(-1)?.streaming, chat.sends],
  () => {
    const el = scroller.value;
    const own = chat.sends !== sentThen;
    sentThen = chat.sends;
    const near = !el || el.scrollHeight - el.scrollTop - el.clientHeight < NEAR;
    if (own || near) void nextTick(scrollDown);
  },
  { flush: "pre" },
);

function quick(text: string): void {
  if (text.startsWith("/")) {
    chat.setDraft(`${text} `); // a command needs its request
    void nextTick(() => composer.value?.focus());
    return;
  }
  void chat.send({ text });
}

onMounted(scrollDown);
</script>

<template>
  <div class="flex h-[calc(100dvh-var(--header-h))] flex-col max-[640px]:h-[calc(100dvh-var(--header-h)-var(--tabbar-h))]">
    <div ref="scroller" data-test="thread-scroll" class="min-h-0 flex-1 overflow-x-hidden overflow-y-auto">
      <div class="sticky top-0 z-[1] bg-background">
        <div class="mx-auto max-w-[780px] px-5 py-2.5 max-[640px]:px-3"><StatusStrip /></div>
      </div>
      <Thread :items="chat.thread" @quick="quick" />
    </div>
    <Composer ref="composer" />
  </div>
</template>
