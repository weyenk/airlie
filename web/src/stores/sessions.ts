import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { createSession, deleteSession, listSessions } from "@/api/sessions";
import type { ChatKind, ThreadRow } from "@/api/sessions";
import { liveResource } from "./live";
import { useTurns } from "./turns";

export type RepoGroup = { repo: string; sessions: ThreadRow[] };

export const LAST_CHAT = "airlie.chat.last";

/** The chat this browser had open last (a convenience: never required, storage may be blocked). */
export function lastChat(): string | null {
  try {
    return localStorage.getItem(LAST_CHAT) || null;
  } catch {
    return null;
  }
}

export function rememberChat(id: string | null): void {
  try {
    if (id) localStorage.setItem(LAST_CHAT, id);
    else localStorage.removeItem(LAST_CHAT);
  } catch {
    // blocked storage: the last chat is just not remembered
  }
}

const newestFirst = (a: ThreadRow, b: ThreadRow): number => b.updated - a.updated || b.created - a.created;

/** The chats, current through the live feed on the `chat` topic. */
export const useSessions = defineStore("sessions", () => {
  const live = liveResource("chat", listSessions);
  const rows = computed<ThreadRow[]>(() => live.data.value ?? []);
  const loaded = computed(() => live.data.value !== undefined);
  const currentId = ref<string | null>(null);

  /** Pinned repositories in pin order, each with its chats (none yet is fine); chats of repositories not pinned
   * under "other" (never dropped: Review Focus 5); plain chats. Newest first inside each. */
  function grouped(pins: string[]): { pinned: RepoGroup[]; other: RepoGroup[]; plain: ThreadRow[] } {
    const sorted = [...rows.value].sort(newestFirst);
    const byRepo = new Map<string, ThreadRow[]>();
    const plain: ThreadRow[] = [];
    for (const row of sorted) {
      if (row.kind === "plain" || !row.repo) plain.push(row);
      else byRepo.set(row.repo, [...(byRepo.get(row.repo) ?? []), row]);
    }
    const pinned = pins.map((repo) => ({ repo, sessions: byRepo.get(repo) ?? [] }));
    const other = [...byRepo].filter(([repo]) => !pins.includes(repo)).map(([repo, sessions]) => ({ repo, sessions }));
    return { pinned, other, plain };
  }

  async function create(kind: ChatKind, repo?: string): Promise<ThreadRow> {
    const row = await createSession(kind, repo);
    await live.refresh();
    return row;
  }

  async function remove(id: string): Promise<void> {
    await deleteSession(id);
    useTurns().forget(id);
    if (lastChat() === id) rememberChat(null);
    await live.refresh();
  }

  return { rows, loaded, error: live.error, refresh: live.refresh, grouped, currentId, create, remove };
});
