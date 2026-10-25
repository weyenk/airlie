<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from "vue";
import type { ChatItem } from "@/stores/chat";
import Bubble from "./Bubble.vue";

const WELCOME =
  "Hi, I'm airlie's assistant. Ask me how airlie works or what's going on. Type / for commands such as /story or /clear.";
const props = defineProps<{ items: ChatItem[] }>();
const emit = defineEmits<{ quick: [text: string] }>();

const QUICK = ["What can you do?", "What's running right now?", "/story"];
const end = ref<HTMLElement | null>(null);

function scrollDown(): void {
  void nextTick(() => end.value?.scrollIntoView?.({ block: "end" }));
}
watch(() => [props.items.length, props.items.at(-1)?.text.length, props.items.at(-1)?.streaming], scrollDown);
onMounted(scrollDown);
</script>

<template>
  <div role="log" aria-label="Conversation" class="mx-auto flex max-w-[780px] flex-col gap-4 px-5 pt-3.5 pb-6 max-[640px]:px-3">
    <div v-if="!items.length">
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
    <Bubble v-for="(item, i) in items" :key="i" :item="item" />
    <div ref="end" />
  </div>
</template>
