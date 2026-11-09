// The builder's unsaved canvas, held while the person is inside a bundle: opening a bundle box leaves this builder
// for the bundle's, and the breadcrumb (or the browser's Back) comes back to this one with its edits. Kept in
// sessionStorage by token, like the half-written card (stores/card_draft.ts), so a reload mid-trip loses nothing.
// A hold nobody takes back is dropped after a day, and only the newest few are kept.
import type { Graph } from "./graph";

/** One canvas held: the workflow's name, its graph as edited and the text of the prompt files it brings. */
export type HeldGraph = { name: string; graph: Graph; prompts: Record<string, string>; up?: Up };
/** Where a held canvas was itself opened from (a bundle inside a bundle): the token of its parent's hold and the
 * parent's name, so coming back to it keeps the breadcrumb going up. */
export type Up = { from: string; parent: string };

type Entry = HeldGraph & { at: number };

const STORAGE = "airlie.builderHold";
/** How long a hold is kept, and how many. */
export const HOLD_MS = 24 * 60 * 60 * 1000;
export const HOLD_MAX = 10;

function load(): Record<string, Entry> {
  try {
    const raw = sessionStorage.getItem(STORAGE);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return pruned(parsed as Record<string, Entry>);
  } catch {
    return {}; // blocked storage or a damaged entry: nothing held
  }
}

/** The holds younger than a day (one with no time is as old as can be), the newest `HOLD_MAX` of them. */
function pruned(held: Record<string, Entry>): Record<string, Entry> {
  const now = Date.now();
  const live = Object.entries(held).filter(([, e]) => typeof e?.at === "number" && now - e.at < HOLD_MS);
  live.sort((a, b) => a[1].at - b[1].at);
  return Object.fromEntries(live.slice(-HOLD_MAX));
}

function store(held: Record<string, Entry>): boolean {
  try {
    sessionStorage.setItem(STORAGE, JSON.stringify(held));
    return true;
  } catch {
    return false; // blocked storage or no room
  }
}

function newToken(): string {
  try {
    return crypto.randomUUID();
  } catch {
    // not a secure context (plain http on the LAN): a token only has to be unique in this tab
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
}

/** Hold the canvas `name` as edited; the token comes back to `takeGraph`. Null when the browser would not keep it
 * (the caller must not leave unsaved edits behind). */
export function holdGraph(name: string, graph: Graph, prompts: Record<string, string>, up?: Up): string | null {
  const token = newToken();
  const held = pruned({ ...load(), [token]: { name, graph, prompts, ...(up ? { up } : {}), at: Date.now() } });
  return store(held) ? token : null;
}

/** The canvas held under `token`, forgotten as it is taken (it is used once); null when there is none (a token
 * from another tab, a cleared session or one a day old). */
export function takeGraph(token: string): HeldGraph | null {
  const held = load();
  const found = held[token];
  delete held[token];
  store(held);
  if (!found) return null;
  return { name: found.name, graph: found.graph, prompts: found.prompts, ...(found.up ? { up: found.up } : {}) };
}
