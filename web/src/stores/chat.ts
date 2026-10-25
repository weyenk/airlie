// Chat's thread: kept in sessionStorage under `airlie.chat` in the format today's app writes (so a thread carries
// over between the apps), the draft under `airlie.chat.draft`, and the streamed reply.
import { defineStore } from "pinia";
import { ref } from "vue";
import { ApiError } from "@/api";
import { StreamCut, stream } from "@/api/chat";
import { storyDiagnosis } from "@/api/workflows";

export const THREAD = "airlie.chat";
export const DRAFT = "airlie.chat.draft";
/** Turns sent back to the model. */
export const HISTORY = 20;

/** What an attachment keeps in the thread: its id and name, never its bytes. */
export type Attachment = { id: string; name: string; mime?: string; size: number; kind: string };
/** A job a story or run started from the chat (today's `started`). */
export type Started = { job_id: string; kind: string; label?: string };

/**
 * One thread item as today's app saves it. `user`: what was typed (`text`) and what went to the model (`sent`: the
 * text without a /skill word). `chat`: the reply; `streaming`, `thinking`, `cut`, `error` describe how it ended;
 * `action` is the skill's action; `storyText` and `started` belong to the /story card. `card`: a job card.
 * Fields this app does not know are kept as they were read.
 */
export type ChatItem = {
  role: "user" | "chat" | "card";
  /** Absent on a `card` (today's app pushes `{role: "card", job_id, kind, label}`). */
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
  const thread = ref<ChatItem[]>(loadThread());
  const draft = ref(String(read<unknown>(DRAFT, "") ?? ""));
  /** One more on every message the person sends: the screen scrolls to it whatever it was showing. */
  const sends = ref(0);
  /** The AbortController of the reply being streamed; null when none is. */
  const busy = ref<AbortController | null>(null);

  function saveThread(): void {
    write(THREAD, thread.value);
  }

  function setDraft(text: string): void {
    draft.value = text;
    write(DRAFT, text);
  }

  function push(item: ChatItem): ChatItem {
    thread.value.push(item);
    saveThread();
    return thread.value[thread.value.length - 1];
  }

  /** The turns the model is sent: the question as `sent` (or as typed), and the replies that have text. */
  function history(): { role: "user" | "assistant"; content: unknown }[] {
    return thread.value
      .filter((m) => m.role === "user" || (m.role === "chat" && m.text))
      .slice(-HISTORY)
      .map((m) => ({ role: m.role === "user" ? "user" : "assistant", content: contentOf(m) }));
  }

  /** A turn as the chat route takes it: text, or text and an `airlie_upload` part per attachment. */
  function contentOf(m: ChatItem): unknown {
    if (m.role !== "user" || !m.attachments?.length) return m.sent ?? m.text;
    const parts: unknown[] = m.sent ? [{ type: "text", text: m.sent }] : [];
    for (const a of m.attachments) parts.push({ type: "airlie_upload", upload: a.id, name: a.name });
    return parts;
  }

  /**
   * /why: the question with the line's diagnosis of the story it names put before it as data (the chat role's
   * `why` skill answers from it). A story with none, or no key in the question, is said so in its place; only a
   * signed-out answer is an error, for `send` to take the turn back.
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

  /**
   * Ask the model; the reply streams into the thread. Does nothing while a reply is still coming. Resolves true
   * when the person was signed out and the turn was taken back (the caller puts its attachments back).
   */
  async function send(opts: SendOptions): Promise<boolean> {
    if (busy.value) return false;
    const text = opts.text;
    const attachments = opts.attachments?.length ? opts.attachments : undefined;
    sends.value += 1;
    const mine = push({ role: "user", text, sent: opts.sent ?? text, ...(attachments ? { attachments } : {}) });
    const reply = push({ role: "chat", text: "", streaming: true, thinking: false, action: opts.skill?.action ?? "" });
    setDraft("");
    const controller = new AbortController();
    busy.value = controller;
    try {
      if (opts.skill?.action === "why") mine.sent = await withDiagnosis(mine.sent ?? text);
      await stream(
        "/v1/chat/completions",
        { model: "chat", messages: history(), stream: true, ...(opts.skill ? { skill: opts.skill.name } : {}) },
        (delta) => {
          reply.text = (reply.text ?? "") + delta;
        },
        {
          signal: controller.signal,
          onReasoning: () => {
            reply.thinking = true;
          },
        },
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        // signed out: the turn is taken back, and what was typed goes back into the box after sign-in
        const i = thread.value.indexOf(mine);
        if (i < 0) return false; // the thread was cleared meanwhile: nothing to take back, nothing to restore
        thread.value.splice(i, 2);
        saveThread();
        setDraft(text);
        return true;
      }
      // a stopped reply is cut like one the connection dropped: what arrived stays, and it says so
      if (e instanceof StreamCut || (e as { name?: string })?.name === "AbortError") reply.cut = true;
      else reply.error = `Can't reach airlie: ${(e as Error).message}`;
    } finally {
      reply.streaming = false;
      if (busy.value === controller) busy.value = null;
      saveThread();
    }
    return false;
  }

  /** The story card's text as edited, kept with the item. */
  function editStory(item: ChatItem, text: string): void {
    item.storyText = text;
    saveThread();
  }

  /** A story the card started: the item shows its job card from now on, and after a reload. */
  function markStarted(item: ChatItem, started: Started): void {
    item.started = started;
    saveThread();
  }

  /** Stop the reply that is streaming; what arrived stays. */
  function stop(): void {
    busy.value?.abort();
  }

  /** /clear: a fresh session. The thread and the draft go; a reply still streaming is stopped. */
  function clear(): void {
    busy.value?.abort();
    busy.value = null;
    thread.value = [];
    saveThread();
    setDraft("");
  }

  return { thread, draft, busy, sends, send, stop, clear, setDraft, push, saveThread, editStory, markStarted };
});
