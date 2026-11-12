<script setup lang="ts">
import { ArrowUp, Plus, Square } from "lucide-vue-next";
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ApiError } from "@/api";
import { chatSkills } from "@/api/chat";
import type { Skill } from "@/api/chat";
import type { ThreadRow } from "@/api/sessions";
import { useChatStore } from "@/stores/chat";
import { useSessionInfo } from "@/stores/sessionInfo";
import { useSessions } from "@/stores/sessions";
import { useTurns } from "@/stores/turns";
import AttachTray from "./AttachTray.vue";
import SlashMenu from "./SlashMenu.vue";
import { clearPreviews, useAttachments } from "./attachments";

defineOptions({ name: "ChatComposer" });

/** The message box of one chat: the text and attachments of a turn, the / menu, the model it runs on and what it asks first. */
const props = defineProps<{ session: ThreadRow }>();
const chat = useChatStore();
const turns = useTurns();
const sessions = useSessions();
const info = useSessionInfo();
const router = useRouter();
const att = useAttachments();
const box = ref<HTMLTextAreaElement | null>(null);
const picker = ref<HTMLInputElement | null>(null);
const skills = ref<Skill[]>([]);
const active = ref(0);
const dismissed = ref(false);
const dropping = ref(false);
const failed = ref("");

watch(
  () => props.session,
  (session) => {
    turns.chatFor(session); // made before `busy` reads its status (the thread above has usually made it)
    info.load(session.id);
  },
  { immediate: true },
);
const model = computed(() => info.label(props.session.id));
const askFirst = computed(() => info.askFirst(props.session.id) ?? []);
const busy = computed(() => ["submitted", "streaming"].includes(turns.status(props.session.id)));

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
const sendOff = computed(() => !att.ready.value);

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

/** /clear: a fresh chat of the same kind (and repository) opens; this one stays in the panel. */
async function fresh(): Promise<void> {
  try {
    const row = await sessions.create(props.session.kind, props.session.repo ?? undefined);
    await router.push(`/chat/${encodeURIComponent(row.id)}`);
  } catch (e) {
    failed.value = e instanceof Error ? e.message : String(e);
  }
}

async function submit(): Promise<void> {
  const text = chat.draft.trim();
  if (busy.value || !att.ready.value) return; // a reply is coming, still uploading, or a chip to remove
  if (!text && !att.pending.value.length) return;
  const hit = skillOf(text);
  if (hit?.skill.action === "clear") {
    att.reset();
    clearPreviews();
    chat.setDraft("");
    box.value?.focus();
    await fresh();
    return;
  }
  if (hit && !hit.rest && !att.pending.value.length) {
    box.value?.focus(); // "/story" alone: say what for
    return;
  }
  failed.value = "";
  const attachments = att.take();
  chat.setDraft("");
  let signedOut = false;
  try {
    const rest = hit ? hit.rest : text;
    const sent = hit?.skill.action === "why" ? await chat.withDiagnosis(rest) : rest;
    await turns.send(props.session.id, {
      text,
      sent,
      ...(hit ? { skill: { name: hit.skill.name, action: hit.skill.action } } : {}),
      attachments,
    });
    const err = turns.error(props.session.id);
    signedOut = err instanceof ApiError && err.status === 401;
  } catch (e) {
    signedOut = e instanceof ApiError && e.status === 401;
    if (!signedOut) failed.value = e instanceof Error ? e.message : String(e);
  }
  if (signedOut) {
    chat.setDraft(text); // what was typed is back in the box; the files go back in the tray
    att.restore(attachments);
  }
}

function onKeydown(ev: KeyboardEvent): void {
  if (ev.isComposing) return; // an IME's Enter confirms its text: it is not ours
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
          class="min-h-[38px] flex-1 resize-none rounded-[19px] border border-input bg-card px-3.5 py-[9px] text-base text-foreground outline-none focus:border-ring"
          @input="onInput"
          @keydown="onKeydown"
          @paste="onPaste"
        />
        <button
          v-if="busy"
          type="button"
          aria-label="Stop"
          title="Stop the reply"
          class="inline-flex size-[38px] flex-none items-center justify-center rounded-full bg-primary text-primary-foreground"
          @click="turns.stop(session.id)"
        >
          <Square class="size-4 fill-current" />
        </button>
        <button
          v-else
          type="submit"
          aria-label="Send"
          :disabled="sendOff"
          class="inline-flex size-[38px] flex-none items-center justify-center rounded-full bg-primary text-primary-foreground disabled:opacity-50"
        >
          <ArrowUp class="size-5" />
        </button>
      </div>
      <p v-if="failed" role="alert" class="m-0 text-[13px] text-danger-foreground">{{ failed }}</p>
      <div v-if="model || askFirst.length" data-test="composer-footer" class="flex flex-wrap items-center gap-1.5 text-xs">
        <span v-if="model" data-model class="rounded-md bg-chip px-1.5 py-0.5 text-chip-foreground">{{ model }}</span>
        <span
          v-if="askFirst.length"
          data-ask-first
          :title="`Asks you first: ${askFirst.join(', ')}`"
          class="rounded-md bg-chip px-1.5 py-0.5 text-chip-foreground"
        >Ask before pushing</span>
      </div>
      <input ref="picker" type="file" multiple hidden aria-label="Files to attach" @change="onPick" />
    </div>
  </form>
</template>
