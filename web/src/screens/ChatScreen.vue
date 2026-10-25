<script setup lang="ts">
import { ArrowUp } from "lucide-vue-next";
import { nextTick, onMounted, ref, watch } from "vue";
import { chatSkills } from "@/api/chat";
import type { Skill } from "@/api/chat";
import { useChatStore } from "@/stores/chat";
import StatusStrip from "./chat/StatusStrip.vue";
import Thread from "./chat/Thread.vue";

const chat = useChatStore();
const box = ref<HTMLTextAreaElement | null>(null);
const scroller = ref<HTMLElement | null>(null);
let skills: Skill[] = [];

/** "/name rest" for a skill airlie has; null when the text is not a skill command. */
function skillOf(text: string): { skill: Skill; rest: string } | null {
  const m = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(text);
  const skill = m && skills.find((s) => s.name === m[1]);
  return skill ? { skill, rest: (m[2] ?? "").trim() } : null;
}

function autosize(): void {
  const el = box.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
}

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

// the box grows with its text, and shrinks back when the draft is sent, cleared or filled in by a quick reply
watch(() => chat.draft, () => void nextTick(autosize));

function onInput(ev: Event): void {
  chat.setDraft((ev.target as HTMLTextAreaElement).value);
}

async function submit(): Promise<void> {
  const text = chat.draft.trim();
  if (chat.busy || !text) return;
  const hit = skillOf(text);
  if (hit?.skill.action === "clear") {
    chat.clear();
    return;
  }
  if (hit && !hit.rest) {
    box.value?.focus(); // "/story" alone: say what for
    return;
  }
  await chat.send(hit ? { text, sent: hit.rest, skill: { name: hit.skill.name, action: hit.skill.action } } : { text });
}

function onKeydown(ev: KeyboardEvent): void {
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
    ev.preventDefault();
    void submit();
  }
}

function quick(text: string): void {
  if (text.startsWith("/")) {
    chat.setDraft(`${text} `); // a command needs its request
    void nextTick(() => box.value?.focus());
    return;
  }
  void chat.send({ text });
}

onMounted(async () => {
  autosize();
  scrollDown();
  if (matchMedia?.("(min-width: 768px)").matches) box.value?.focus();
  try {
    skills = await chatSkills();
  } catch {
    skills = []; // "/x" is then sent as it was typed
  }
});
</script>

<template>
  <div class="flex h-[calc(100dvh-var(--header-h))] flex-col max-[640px]:h-[calc(100dvh-var(--header-h)-var(--tabbar-h))]">
    <div ref="scroller" data-test="thread-scroll" class="min-h-0 flex-1 overflow-x-hidden overflow-y-auto">
      <div class="sticky top-0 z-[1] bg-background">
        <div class="mx-auto max-w-[780px] px-5 py-2.5 max-[640px]:px-3"><StatusStrip /></div>
      </div>
      <Thread :items="chat.thread" @quick="quick" />
    </div>
    <form aria-label="Message airlie" class="border-t border-border bg-background" @submit.prevent="submit">
      <div class="mx-auto flex max-w-[780px] items-end gap-2 px-5 pt-2.5 pb-3.5 max-[640px]:px-3 max-[640px]:pt-2 max-[640px]:pb-2.5">
        <textarea
          id="msg"
          ref="box"
          :value="chat.draft"
          rows="1"
          aria-label="Message"
          placeholder="Ask airlie, or type / for commands…"
          class="min-h-[38px] flex-1 resize-none rounded-[19px] border border-input bg-card px-3.5 py-[9px] text-foreground outline-none focus:border-ring"
          @input="onInput"
          @keydown="onKeydown"
        />
        <button
          type="submit"
          aria-label="Send"
          :disabled="!!chat.busy"
          class="inline-flex size-[38px] flex-none items-center justify-center rounded-full bg-primary text-primary-foreground disabled:opacity-50"
        >
          <ArrowUp class="size-5" />
        </button>
      </div>
    </form>
  </div>
</template>
