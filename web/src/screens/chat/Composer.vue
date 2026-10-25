<script setup lang="ts">
import { ArrowUp, Plus } from "lucide-vue-next";
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { chatSkills } from "@/api/chat";
import type { Skill } from "@/api/chat";
import { useChatStore } from "@/stores/chat";
import AttachTray from "./AttachTray.vue";
import SlashMenu from "./SlashMenu.vue";
import { clearPreviews, useAttachments } from "./attachments";

defineOptions({ name: "ChatComposer" });

const chat = useChatStore();
const att = useAttachments();
const box = ref<HTMLTextAreaElement | null>(null);
const picker = ref<HTMLInputElement | null>(null);
const skills = ref<Skill[]>([]);
const active = ref(0);
const dismissed = ref(false);
const dropping = ref(false);

/** "/name rest" for a skill airlie has; null when the text is not a skill command. */
function skillOf(text: string): { skill: Skill; rest: string } | null {
  const m = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(text);
  const skill = m && skills.value.find((s) => s.name === m[1]);
  return skill ? { skill, rest: (m[2] ?? "").trim() } : null;
}

// the / menu: only while the first word is being typed, and only when airlie has commands to offer
const typing = computed(() => /^\/(\S*)$/.exec(chat.draft));
const shown = computed(() => {
  const prefix = typing.value?.[1].toLowerCase();
  return prefix === undefined ? [] : skills.value.filter((s) => s.name.startsWith(prefix));
});
const menuOpen = computed(() => !!typing.value && !dismissed.value && skills.value.length > 0);
const cap = computed(() => Math.max(shown.value.length - 1, 0));
const sendOff = computed(() => !!chat.busy || !att.ready.value);

function autosize(): void {
  const el = box.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
}
// the box grows with its text, and shrinks back when the draft is sent, cleared or filled in by a quick reply
watch(() => chat.draft, () => void nextTick(autosize));

function onInput(ev: Event): void {
  dismissed.value = false;
  active.value = 0;
  chat.setDraft((ev.target as HTMLTextAreaElement).value);
}

function choose(s: Skill): void {
  chat.setDraft(`/${s.name} `);
  box.value?.focus();
}

async function submit(): Promise<void> {
  const text = chat.draft.trim();
  if (chat.busy || !att.ready.value) return; // still uploading, or a chip to remove
  if (!text && !att.pending.value.length) return;
  const hit = skillOf(text);
  if (hit?.skill.action === "clear") {
    att.reset();
    clearPreviews();
    chat.clear();
    box.value?.focus();
    return;
  }
  if (hit && !hit.rest && !att.pending.value.length) {
    box.value?.focus(); // "/story" alone: say what for
    return;
  }
  const attachments = att.take();
  const signedOut = await chat.send({
    text,
    sent: hit ? hit.rest : text,
    ...(hit ? { skill: { name: hit.skill.name, action: hit.skill.action } } : {}),
    attachments,
  });
  if (signedOut) att.restore(attachments); // what was typed is back in the box; the files go back in the tray
}

function onKeydown(ev: KeyboardEvent): void {
  if (menuOpen.value && shown.value.length) {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      active.value = (active.value + (ev.key === "ArrowDown" ? 1 : shown.value.length - 1)) % shown.value.length;
      return;
    }
    const top = shown.value[Math.min(active.value, cap.value)];
    // a command typed in full goes through instead of being spent on choosing it
    if ((ev.key === "Enter" || ev.key === "Tab") && !(ev.key === "Enter" && chat.draft.trim() === `/${top.name}`)) {
      ev.preventDefault();
      choose(top);
      return;
    }
  }
  if (menuOpen.value && ev.key === "Escape") {
    ev.preventDefault();
    dismissed.value = true;
    return;
  }
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
    ev.preventDefault();
    void submit();
  }
}

function onPick(): void {
  const input = picker.value;
  if (!input) return;
  att.addFiles([...(input.files ?? [])]);
  input.value = ""; // the same file can be picked again
}

function hasFiles(ev: DragEvent): boolean {
  return [...(ev.dataTransfer?.types ?? [])].includes("Files");
}
function onDragOver(ev: DragEvent): void {
  if (!hasFiles(ev)) return;
  ev.preventDefault();
  dropping.value = true;
}
function onDrop(ev: DragEvent): void {
  dropping.value = false;
  const files = [...(ev.dataTransfer?.files ?? [])];
  if (!files.length) return;
  ev.preventDefault();
  att.addFiles(files);
}
function onPaste(ev: ClipboardEvent): void {
  const files = [...(ev.clipboardData?.files ?? [])];
  if (!files.length) return;
  ev.preventDefault();
  att.addFiles(files);
}

function focus(): void {
  box.value?.focus();
}
defineExpose({ focus });

onMounted(async () => {
  autosize();
  if (matchMedia?.("(min-width: 768px)").matches) box.value?.focus();
  void att.loadLimits();
  try {
    skills.value = await chatSkills();
  } catch {
    skills.value = []; // no menu; "/x" is then sent as it was typed
  }
});
</script>

<template>
  <form
    aria-label="Message airlie"
    :class="['border-t border-border bg-background', dropping ? 'bg-muted' : '']"
    @submit.prevent="submit"
    @dragover="onDragOver"
    @dragleave="dropping = false"
    @drop="onDrop"
  >
    <div class="relative mx-auto flex max-w-[780px] flex-col gap-2 px-5 pt-2.5 pb-3.5 max-[640px]:px-3 max-[640px]:pt-2 max-[640px]:pb-2.5">
      <SlashMenu v-if="menuOpen" :skills="shown" :active="Math.min(active, cap)" @choose="choose" />
      <AttachTray v-if="att.pending.value.length && !menuOpen" :items="att.pending.value" @remove="att.remove" />
      <div class="flex items-end gap-2">
        <button
          type="button"
          aria-label="Attach a file"
          title="Attach a picture, video or file"
          class="inline-flex size-[38px] flex-none items-center justify-center rounded-full border border-border bg-card text-foreground hover:bg-muted"
          @click="picker?.click()"
        >
          <Plus class="size-5" />
        </button>
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
          @paste="onPaste"
        />
        <button
          type="submit"
          aria-label="Send"
          :disabled="sendOff"
          class="inline-flex size-[38px] flex-none items-center justify-center rounded-full bg-primary text-primary-foreground disabled:opacity-50"
        >
          <ArrowUp class="size-5" />
        </button>
      </div>
      <input ref="picker" type="file" multiple hidden aria-label="Files to attach" @change="onPick" />
    </div>
  </form>
</template>
