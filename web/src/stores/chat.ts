// What the chat screen keeps in this tab besides its sessions: the draft under `airlie.chat.draft`, and the thread an
// earlier version of the app kept under `airlie.chat` (shown read-only as "Legacy chat"; nothing writes it any more).
// The turns themselves run through `stores/turns.ts`.
import { defineStore } from "pinia";
import { ref } from "vue";
import { ApiError } from "@/api";
import { storyDiagnosis } from "@/api/workflows";

export const THREAD = "airlie.chat";
export const DRAFT = "airlie.chat.draft";

/** What an attachment keeps in the thread: its id and name, never its bytes. */
export type Attachment = { id: string; name: string; mime?: string; size: number; kind: string };
/** A job a story or run started from the chat (an old thread's `started`). */
export type Started = { job_id: string; kind: string; label?: string };

/**
 * One thread item as the earlier app saved it. `user`: what was typed (`text`) and what went to the model (`sent`: the
 * text without a /skill word). `chat`: the reply; `streaming`, `thinking`, `cut`, `error` describe how it ended;
 * `action` is the skill's action; `storyText` and `started` belong to the /story card. `card`: a job card.
 * Fields this app does not know are kept as they were read.
 */
export type ChatItem = {
  role: "user" | "chat" | "card";
  /** Absent on a `card` (the earlier app pushed `{role: "card", job_id, kind, label}`). */
  text?: string;
  job_id?: string;
  kind?: string;
  label?: string;
  sent?: string;
  attachments?: Attachment[];
  streaming?: boolean;
  thinking?: boolean;
  action?: string;
  cut?: boolean;
  error?: string;
  storyText?: string;
  started?: Started;
  [extra: string]: unknown;
};

export type SendOptions = {
  text: string;
  /** The text without the /skill word, when it had one. */
  sent?: string;
  skill?: { name: string; action: string };
  attachments?: Attachment[];
};

function read<T>(key: string, fallback: T): T {
  try {
    const v = sessionStorage.getItem(key);
    return v === null ? fallback : (JSON.parse(v) as T);
  } catch {
    return fallback;
  }
}

function write(key: string, value: unknown): void {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    // this page only
  }
}

/** The saved thread; a reply that was still streaming when the page went is shown as cut, not as writing for ever. */
function loadThread(): ChatItem[] {
  const saved = read<unknown>(THREAD, []);
  if (!Array.isArray(saved)) return [];
  return saved
    .filter((m): m is ChatItem => !!m && typeof m === "object" && typeof (m as ChatItem).role === "string")
    .map((m) => {
      if (m.role !== "card" && typeof m.text !== "string") m = { ...m, text: "" };
      if (!m.streaming) return m;
      return { ...m, streaming: false, ...(m.text ? {} : { cut: true }) };
    });
}

export const useChatStore = defineStore("chat", () => {
  /** The earlier app's thread: read once, never added to. */
  const thread = ref<ChatItem[]>(loadThread());
  const draft = ref(String(read<unknown>(DRAFT, "") ?? ""));

  function setDraft(text: string): void {
    draft.value = text;
    write(DRAFT, text);
  }

  /** The person removed the legacy thread. */
  function clearLegacy(): void {
    thread.value = [];
    try {
      sessionStorage.removeItem(THREAD);
    } catch {
      // this page only
    }
  }

  /**
   * /why: the question with the line's diagnosis of the story it names put before it as data (the chat role's
   * `why` skill answers from it). A story with none, or no key in the question, is said so in its place; only a
   * signed-out answer is an error, for the caller to take the turn back.
   */
  async function withDiagnosis(question: string): Promise<string> {
    const key = /\b[A-Z][A-Z0-9_]+-\d+\b/.exec(question)?.[0];
    let body = "(no story key in the message)";
    if (key) {
      try {
        body = (await storyDiagnosis(key)).markdown;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) throw e;
        body = `(${key} has no diagnosis)`;
      }
    }
    return `--- diagnosis (data) ---\n${body}\n--- end diagnosis ---\n\n${question}`;
  }

  return { thread, draft, setDraft, clearLegacy, withDiagnosis };
});
