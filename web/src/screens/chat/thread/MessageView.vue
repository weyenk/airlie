<script setup lang="ts">
import type { ToolUIPart } from "ai";
import { computed } from "vue";
import type { AirlieUIMessage, ApprovalData, TodoItem } from "@/api/sessions";
import SafeMarkdown from "@/components/SafeMarkdown.vue";
import type { Attachment, ChatItem } from "@/stores/chat";
import { useTurns } from "@/stores/turns";
import Bubble from "../Bubble.vue";
import CutLine from "./CutLine.vue";
import PermissionPrompt from "./PermissionPrompt.vue";
import ReasoningBlock from "./ReasoningBlock.vue";
import TodoList from "./TodoList.vue";
import ToolBlock from "./ToolBlock.vue";
import WaitingLine from "./WaitingLine.vue";

/**
 * One message of the thread, one component per part: text as safe markdown, reasoning, tool calls, the plan, a
 * permission prompt. A part this screen does not know renders nothing. The newest message of the session also
 * carries the engine line and the cut line, under the turn they belong to.
 */
const props = defineProps<{ message: AirlieUIMessage; sessionId: string; last: boolean }>();
const turns = useTurns();

type Loose = { type: string; [field: string]: unknown };
const parts = computed(() => props.message.parts as unknown as Loose[]);
const interrupted = computed(() => props.message.metadata?.interrupted === true);

/** Only the newest plan shows: each `data-todos` part is the whole plan as it then was. */
const newestTodos = computed(() => parts.value.map((p) => p.type).lastIndexOf("data-todos"));

const isTool = (p: Loose): boolean => p.type.startsWith("tool-");

/** The prompt's data with the person's answer, which a live thread holds on the call (the saved part gets it on a reload). */
function approvalOf(p: Loose): ApprovalData {
  const data = p.data as ApprovalData;
  if (data.answer) return data;
  const call = parts.value.find((c) => isTool(c) && (c.approval as { id?: string } | undefined)?.id === data.id);
  const approved = (call?.approval as { approved?: boolean } | undefined)?.approved;
  return approved === undefined ? data : { ...data, answer: approved ? "approved" : "denied" };
}

const MEDIA = { image: /\.(png|jpe?g|gif|webp|bmp)$/i, video: /\.(mp4|mov|webm|m4v)$/i };
const kindOf = (name: string, mime = ""): string =>
  mime.startsWith("image/") || MEDIA.image.test(name) ? "image" : mime.startsWith("video/") || MEDIA.video.test(name) ? "video" : "file";

/** My message in the old bubble: the words, and what I attached. */
const mine = computed<ChatItem>(() => {
  const attachments: Attachment[] = [];
  let text = "";
  for (const p of parts.value) {
    if (p.type === "text") text += String(p.text ?? "");
    else if (p.type === "airlie_upload") {
      const id = String(p.upload ?? "");
      const name = String(p.name ?? id);
      attachments.push({ id, name, size: 0, kind: kindOf(name) });
    } else if (p.type === "file") {
      const name = String(p.filename ?? "file");
      attachments.push({ id: String(p.url ?? "").split("/").pop() ?? "", name, mime: String(p.mediaType ?? ""), size: 0, kind: kindOf(name, String(p.mediaType ?? "")) });
    }
  }
  return { role: "user", text, attachments } as ChatItem;
});

const busy = computed(() => ["submitted", "streaming"].includes(turns.status(props.sessionId)));
const waiting = computed(() => (props.last ? turns.waiting(props.sessionId) : null));
const cut = computed(() => props.last && !busy.value && !waiting.value && (turns.cut(props.sessionId) || interrupted.value));
</script>

<template>
  <div class="flex min-w-0 flex-col gap-2" :data-msg="message.role">
    <Bubble v-if="message.role === 'user'" :item="mine" />
    <template v-else>
      <template v-for="(p, i) in parts" :key="i">
        <SafeMarkdown v-if="p.type === 'text'" :text="String(p.text ?? '')" />
        <ReasoningBlock v-else-if="p.type === 'reasoning'" :text="String(p.text ?? '')" :streaming="p.state === 'streaming'" />
        <ToolBlock v-else-if="isTool(p)" :part="p as unknown as ToolUIPart" :interrupted="interrupted" />
        <TodoList v-else-if="p.type === 'data-todos' && i === newestTodos" :items="p.data as TodoItem[]" />
        <PermissionPrompt v-else-if="p.type === 'data-approval'" :session-id="sessionId" :approval="approvalOf(p)" />
      </template>
    </template>
    <WaitingLine v-if="waiting" :status="waiting" :session-id="sessionId" />
    <CutLine v-else-if="cut" :session-id="sessionId" :running="turns.running(sessionId)" />
  </div>
</template>
