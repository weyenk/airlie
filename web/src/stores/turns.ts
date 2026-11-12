// Chat turns on the AI SDK: one `Chat` per session (`@ai-sdk/vue`) over `POST /v1/airlie/chat/turn`, which answers with
// the UI message stream. The store adds what the SDK does not know: a stream cut without `finish`, the engine's waiting
// line, a refused answer, the context readout, and the saved thread read back after a cut.

import { Chat } from "@ai-sdk/vue";
import { DefaultChatTransport } from "ai";
import { defineStore } from "pinia";
import { reactive, watch } from "vue";
import { ApiError, authHeaders, detailOfText, signalUnauthorized } from "@/api";
import { sseEvents } from "@/api/chat";
import type { EngineStatus } from "@/api/engines";
import { getSession, stopSession, toUIMessage } from "@/api/sessions";
import type { AirlieUIMessage, EngineData, ThreadDetail, ThreadRow, TodoItem } from "@/api/sessions";
import type { SendOptions } from "./chat";
import { useFeedStore } from "./feed";

const TURN = "/v1/airlie/chat/turn";
export const INTERRUPTED = "interrupted: the connection was cut before this call finished";

type Approval = { id: string; approved: boolean };

/** What the store knows of one session besides its Chat. */
type SessionState = {
  row: ThreadRow;
  /** Requests begun: a history read that began before the newest one is stale. */
  requests: number;
  loading: Promise<unknown>;
  loadError: Error | null;
  /** Whether the newest stream ended with `[DONE]` (the server finished it, by finish or by an error chunk); undefined: no stream. */
  scan: Promise<boolean> | undefined;
  stopping: boolean;
  cut: boolean;
  engine: EngineStatus | null;
  /** How much the Chat held when the engine row came: the line goes once more arrives. */
  engineAt: number;
  todos: TodoItem[];
  refused: Record<string, string>;
};

function rowOfEngine(data: EngineData): EngineStatus {
  return {
    name: data.engine,
    kind: data.engine.startsWith("vm:") ? "vm" : "engine",
    state: data.state,
    detail: data.detail,
    progress: data.progress ?? null,
    holders: [],
    lifecycle: "",
    roles: [],
  };
}

/** A number that only grows while the newest message gets content or a message is added. */
function contentSize(chat: Chat<AirlieUIMessage>): number {
  const messages = chat.messages;
  const last = messages.at(-1);
  let size = messages.length * 1e9;
  for (const p of last?.parts ?? []) size += 1 + (typeof (p as { text?: unknown }).text === "string" ? (p as { text: string }).text.length : 0);
  return size;
}

const busy = (chat: Chat<AirlieUIMessage>): boolean => chat.status === "submitted" || chat.status === "streaming";

/** The message with every call that was still open marked as interrupted, as the server saves a stopped one. */
function interrupted(message: AirlieUIMessage): AirlieUIMessage {
  const open = new Set(["input-streaming", "input-available", "approval-requested", "approval-responded"]);
  const parts = message.parts.map((p) => {
    const part = p as { type: string; state?: string };
    if (!part.type.startsWith("tool-") || !part.state || !open.has(part.state)) return p;
    return { ...p, state: "output-error", errorText: INTERRUPTED } as typeof p;
  });
  return { ...message, parts, metadata: { ...message.metadata, interrupted: true } };
}

export const useTurns = defineStore("turns", () => {
  const chats = new Map<string, Chat<AirlieUIMessage>>();
  const sessions = reactive<Record<string, SessionState>>({});
  const feed = useFeedStore();

  /** Resolves at the next `chat` hint (they carry no key) or when the feed reconnects (changes between are not replayed). */
  function nextHint(): Promise<void> {
    return new Promise((resolve) => {
      let stop = (): void => {};
      const done = (): void => {
        off();
        stop();
        resolve();
      };
      const off = feed.on("chat", done);
      stop = watch(() => feed.generation, done);
    });
  }

  function begin(s: SessionState): void {
    s.requests += 1;
    s.scan = undefined;
    s.cut = false;
    s.stopping = false;
    s.engine = null;
  }

  /** Read the saved thread into the Chat, unless a turn has begun since the read did: its stream is the newer. */
  async function load(id: string, keepPartial = false): Promise<ThreadDetail | undefined> {
    const chat = chats.get(id);
    const s = sessions[id];
    if (!chat || !s) return undefined;
    const mark = s.requests;
    const detail = await getSession(id);
    // `keepPartial`: the server still runs the turn this client lost, so the saved thread has not got its reply yet
    if (s.requests !== mark || busy(chat) || (keepPartial && detail.running)) return detail;
    chat.messages = detail.messages.map(toUIMessage);
    s.todos = detail.todos;
    return detail;
  }

  /** After a cut: read the saved thread, and while the server still runs the turn wait for its `chat` hint and read again. */
  async function settle(id: string): Promise<void> {
    for (;;) {
      let detail: ThreadDetail | undefined;
      try {
        detail = await load(id, true);
      } catch {
        return; // airlie is out of reach: the partial reply stays as it arrived
      }
      if (!detail?.running) return;
      await nextHint();
    }
  }

  async function ended(id: string, isAbort: boolean): Promise<void> {
    const chat = chats.get(id);
    const s = sessions[id];
    if (!chat || !s || isAbort || s.stopping || !s.scan) return;
    if (await s.scan) return; // the server finished the stream (an error chunk is an error, not a cut)
    s.cut = true;
    const last = chat.messages.at(-1);
    if (last?.role === "assistant") chat.messages = [...chat.messages.slice(0, -1), interrupted(last)];
    await settle(id);
  }

  function transportFor(s: SessionState): DefaultChatTransport<AirlieUIMessage> {
    return new DefaultChatTransport<AirlieUIMessage>({
      api: TURN,
      headers: () => authHeaders(),
      fetch: async (input, init) => {
        const response = await fetch(input, init);
        if (response.status === 401) signalUnauthorized();
        if (!response.ok) throw new ApiError(response.status, detailOfText(await response.text().catch(() => "")));
        if (!response.body) return response;
        const [read, passed] = response.body.tee();
        s.scan = (async () => {
          let done = false;
          try {
            for await (const event of sseEvents(read)) if (event === "[DONE]") done = true;
          } catch {
            // the connection failed: no [DONE]
          }
          return done;
        })();
        return new Response(passed, { status: response.status, headers: response.headers });
      },
      prepareSendMessagesRequest: ({ id, messages, body }) => {
        const { approvals, skill } = (body ?? {}) as { approvals?: Approval[]; skill?: string };
        return {
          body: {
            id,
            kind: s.row.kind,
            repo: s.row.repo ?? undefined,
            base_ref: s.row.base_ref ?? undefined,
            skill,
            approvals,
            // an answer carries no new message of the person's; a send (and a retry) only the newest one
            messages: approvals?.length ? [] : messages.slice(-1),
          },
        };
      },
    });
  }

  /** The session's Chat, made once per id; its history is read from the server. */
  function chatFor(session: ThreadRow): Chat<AirlieUIMessage> {
    const known = chats.get(session.id);
    if (known) return known;
    sessions[session.id] = {
      row: session,
      requests: 0,
      loading: Promise.resolve(),
      loadError: null,
      scan: undefined,
      stopping: false,
      cut: false,
      engine: null,
      engineAt: 0,
      todos: [],
      refused: {},
    };
    const s = sessions[session.id];
    const chat = new Chat<AirlieUIMessage>({
      id: session.id,
      transport: transportFor(s),
      onData: (part) => {
        if (part.type === "data-engine") {
          s.engine = rowOfEngine(part.data);
          s.engineAt = contentSize(chat);
        } else if (part.type === "data-todos") {
          s.todos = part.data as TodoItem[];
        }
      },
      onFinish: ({ isAbort }) => {
        void ended(session.id, isAbort);
      },
    });
    chats.set(session.id, chat);
    s.loading = load(session.id).catch((e: unknown) => {
      s.loadError = e instanceof Error ? e : new Error(String(e));
    });
    return chat;
  }

  function existing(id: string): { chat: Chat<AirlieUIMessage>; s: SessionState } {
    const chat = chats.get(id);
    if (!chat) throw new Error(`no chat ${id}: chatFor(session) first`);
    return { chat, s: sessions[id] };
  }

  /** Refetch the history into the Chat (only while it is not streaming). */
  async function reload(id: string): Promise<void> {
    await load(id);
  }

  async function send(id: string, opts: SendOptions): Promise<void> {
    const { chat, s } = existing(id);
    await s.loading;
    begin(s);
    const files = (opts.attachments ?? []).map((a) => ({
      type: "file" as const,
      mediaType: a.mime || "application/octet-stream",
      filename: a.name,
      url: `/v1/airlie/uploads/${a.id}`,
    }));
    await chat.sendMessage({ text: opts.sent ?? opts.text, files }, opts.skill ? { body: { skill: opts.skill.name } } : undefined);
  }

  /** Stop the turn: the server cancels it and saves what it had; then the Chat stops reading. */
  async function stop(id: string): Promise<void> {
    const { chat, s } = existing(id);
    s.stopping = true; // the stream the server ends on a Stop is not a cut
    try {
      await stopSession(id);
    } finally {
      await chat.stop();
    }
    await load(id).catch(() => undefined);
  }

  /** Resend the newest message of the person's (its own id: the server keeps it once). */
  async function retry(id: string): Promise<void> {
    const { chat, s } = existing(id);
    await s.loading;
    begin(s);
    await chat.regenerate();
  }

  /** Answer a permission prompt: no new message, the answer alone; the reply streams onto the asked message. */
  async function answer(id: string, approvalId: string, approved: boolean): Promise<void> {
    const { chat, s } = existing(id);
    begin(s);
    delete s.refused[approvalId];
    await chat.addToolApprovalResponse({ id: approvalId, approved });
    await chat.sendMessage(undefined, { body: { approvals: [{ id: approvalId, approved }] satisfies Approval[] } });
    if (chat.status === "error" && chat.error instanceof ApiError && chat.error.status === 409) {
      s.refused[approvalId] = chat.error.message;
      await load(id).catch(() => undefined); // the saved thread says whether the prompt still waits
    }
  }

  const status = (id: string): "ready" | "submitted" | "streaming" | "error" => chats.get(id)?.status ?? "ready";

  /** The saved thread ends in a missing or an interrupted reply: a Retry has something to resend. */
  function canRetry(id: string): boolean {
    const last = chats.get(id)?.messages.at(-1);
    if (!last) return false;
    return last.role === "user" || last.metadata?.interrupted === true;
  }

  function waiting(id: string): EngineStatus | null {
    const chat = chats.get(id);
    const s = sessions[id];
    if (!chat || !s?.engine || !busy(chat)) return null;
    return contentSize(chat) > s.engineAt ? null : s.engine;
  }

  function usage(id: string): { used: number; limit: number } | null {
    const messages = chats.get(id)?.messages ?? [];
    for (let i = messages.length - 1; i >= 0; i--) {
      const u = messages[i].metadata?.usage;
      if (u) return { used: u.prompt_tokens + u.completion_tokens, limit: u.max_context };
    }
    return null;
  }

  return {
    chatFor,
    reload,
    send,
    stop,
    retry,
    answer,
    status,
    canRetry,
    waiting,
    usage,
    /** The turn's last cut: the stream ended without `finish` and not by Stop. */
    cut: (id: string): boolean => sessions[id]?.cut ?? false,
    /** The answer to an answer that the server refused (409), shown on the prompt. */
    refused: (id: string, approvalId: string): string | null => sessions[id]?.refused[approvalId] ?? null,
    todos: (id: string): TodoItem[] => sessions[id]?.todos ?? [],
    error: (id: string): Error | undefined => chats.get(id)?.error,
    loadError: (id: string): Error | null => sessions[id]?.loadError ?? null,
  };
});
