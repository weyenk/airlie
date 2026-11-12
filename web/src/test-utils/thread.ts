// Test helper: a server for one chat session's history and Stop, for the thread parts that act through the turns store.

import { createPinia, setActivePinia } from "pinia";
import type { StoredMessage, StoredPart, ThreadDetail, ThreadRow } from "@/api/sessions";
import { useTurns } from "@/stores/turns";

export const ROW: ThreadRow = {
  id: "chat-1",
  kind: "repository",
  title: "hi",
  repo: "acme/api",
  base_ref: "main",
  branch: "airlie/chat-abc",
  running: false,
  created: 1,
  updated: 2,
};

export const message = (id: string, role: "user" | "assistant", parts: StoredPart[], interrupted = false): StoredMessage => ({
  id,
  role,
  parts,
  created: 1,
  usage: null,
  interrupted,
});

export type Server = { calls: { url: string; method: string; body: unknown }[]; detail: ThreadDetail; turn?: () => Response | Promise<Response> };

/** Pinia and a fetch that answers the session's history from `server.detail`, a turn from `server.turn`. */
export function startThread(messages: StoredMessage[] = [], over: Partial<ThreadDetail> = {}): Server {
  setActivePinia(createPinia());
  const server: Server = { calls: [], detail: { ...ROW, todos: [], messages, ...over } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const raw = init?.body;
      server.calls.push({ url, method: init?.method ?? "GET", body: typeof raw === "string" ? JSON.parse(raw) : undefined });
      if (url === "/v1/airlie/chat/turn") return server.turn ? server.turn() : new Response(null, { status: 500 });
      if (url.endsWith("/stop")) return new Response(null, { status: 204 });
      return Response.json(server.detail);
    }),
  );
  return server;
}

/** The session's Chat made and its saved history read in. */
export async function openChat(): Promise<ReturnType<typeof useTurns>> {
  const turns = useTurns();
  turns.chatFor(ROW);
  await turns.reload(ROW.id);
  return turns;
}
