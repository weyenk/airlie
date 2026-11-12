// Chat sessions (`/v1/airlie/chat/threads`, pins, the repositories a person may pin) and a stored message as the
// client's UI message. The turn itself streams through `stores/turns.ts` on the AI SDK.

import type { UIMessage } from "ai";
import { request } from "./index";
import type { EngineStatus } from "./engines";

export type ChatKind = "plain" | "repository";

/** A chat in the list (`running`: a turn is in flight; times are epoch seconds). */
export type ThreadRow = {
  id: string;
  kind: ChatKind;
  title: string;
  repo: string | null;
  base_ref: string | null;
  /** "airlie/chat-<12 hex>" for a repository chat. */
  branch: string | null;
  running: boolean;
  created: number;
  updated: number;
};

export type Usage = { prompt_tokens: number; completion_tokens: number; cached_tokens: number; max_context: number };

export type TodoItem = { id: string; text: string; status: "pending" | "in_progress" | "done" };

export type ApprovalData = {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  /** Only in a stored part, once answered; "failed": approved, and the call it ran failed (`error` says how). */
  answer?: "approved" | "denied" | "failed";
  error?: string;
  /** The server no longer waits on this request (serve restarted since it was asked). */
  expired?: boolean;
};

/** A part as the server stores it: the AI SDK's UI parts, plus `airlie_upload` (`{upload, name?}`) on a user message. */
export type StoredPart = { type: string; [field: string]: unknown };

export type StoredMessage = {
  id: string;
  role: "user" | "assistant";
  parts: StoredPart[];
  created: number;
  usage: Usage | null;
  interrupted: boolean;
};

export type ThreadDetail = ThreadRow & { todos: TodoItem[]; messages: StoredMessage[] };

export type ChatMeta = { usage?: Usage; interrupted?: boolean; created?: number };
/** The `data-engine` payload: the manager's row for the engine or VM the turn waits for (`engine` is its name). */
export type EngineData = { engine: string; state: EngineStatus["state"]; detail: string; progress: EngineStatus["progress"] };
export type ChatData = { approval: ApprovalData; engine: EngineData; todos: TodoItem[] };
export type AirlieUIMessage = UIMessage<ChatMeta, ChatData>;

const THREADS = "/v1/airlie/chat/threads";

const repoPath = (repo: string): string => repo.split("/").map(encodeURIComponent).join("/");

export function listSessions(kind?: ChatKind): Promise<ThreadRow[]> {
  return request<ThreadRow[]>(kind ? `${THREADS}?kind=${kind}` : THREADS);
}

export function createSession(kind: ChatKind, repo?: string, baseRef?: string): Promise<ThreadRow> {
  return request<ThreadRow>(THREADS, { method: "POST", body: { kind, repo, base_ref: baseRef } });
}

export function getSession(id: string): Promise<ThreadDetail> {
  return request<ThreadDetail>(`${THREADS}/${encodeURIComponent(id)}`);
}

export function deleteSession(id: string): Promise<void> {
  return request<void>(`${THREADS}/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function stopSession(id: string): Promise<void> {
  return request<void>(`${THREADS}/${encodeURIComponent(id)}/stop`, { method: "POST" });
}

export async function listPins(): Promise<string[]> {
  return (await request<{ pins: string[] }>("/v1/airlie/chat/pins")).pins;
}

export function pinRepository(repo: string): Promise<void> {
  return request<void>(`/v1/airlie/chat/pins/${repoPath(repo)}`, { method: "PUT" });
}

export function unpinRepository(repo: string): Promise<void> {
  return request<void>(`/v1/airlie/chat/pins/${repoPath(repo)}`, { method: "DELETE" });
}

/** The repositories airlie knows ("owner/name"): what a person may pin. */
export async function listRepositories(): Promise<string[]> {
  return (await request<{ repos: string[] }>("/v1/airlie/repos/names")).repos;
}

/**
 * A stored message as the client's UI message: its parts stay as they are (`airlie_upload` parts too: SentFile
 * renders them), usage and interrupted go to `metadata`.
 */
export function toUIMessage(m: StoredMessage): AirlieUIMessage {
  const metadata: ChatMeta = { created: m.created };
  if (m.usage) metadata.usage = m.usage;
  if (m.interrupted) metadata.interrupted = true;
  return { id: m.id, role: m.role, parts: m.parts as unknown as AirlieUIMessage["parts"], metadata };
}
