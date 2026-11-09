// The builder's unsaved canvas, held while the person is inside a bundle: opening a bundle box leaves this builder
// for the bundle's, and the breadcrumb comes back to this one with its edits. Kept in sessionStorage by token, like
// the half-written card (stores/card_draft.ts), so a reload mid-trip loses nothing.
import type { Graph } from "./graph";

/** One canvas held: the workflow's name, its graph as edited and the text of the prompt files it brings. */
export type HeldGraph = { name: string; graph: Graph; prompts: Record<string, string>; up?: Up };
/** Where a held canvas was itself opened from (a bundle inside a bundle): the token of its parent's hold and the
 * parent's name, so coming back to it keeps the breadcrumb going up. */
export type Up = { from: string; parent: string };

const STORAGE = "airlie.builderHold";

function load(): Record<string, HeldGraph> {
  try {
    const raw = sessionStorage.getItem(STORAGE);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, HeldGraph>) : {};
  } catch {
    return {}; // blocked storage or a damaged entry: nothing held
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

/** Hold the canvas `name` as edited; the token comes back to `takeGraph`. */
export function holdGraph(name: string, graph: Graph, prompts: Record<string, string>, up?: Up): string {
  const token = newToken();
  const held = { ...load(), [token]: { name, graph, prompts, ...(up ? { up } : {}) } };
  try {
    sessionStorage.setItem(STORAGE, JSON.stringify(held));
  } catch {
    // blocked storage: nothing held, and takeGraph says so
  }
  return token;
}

/** The canvas held under `token`, forgotten as it is taken (it is used once); null when there is none (a token
 * from another tab or a cleared session). */
export function takeGraph(token: string): HeldGraph | null {
  const held = load();
  const found = held[token];
  if (!found) return null;
  delete held[token];
  try {
    sessionStorage.setItem(STORAGE, JSON.stringify(held));
  } catch {
    // blocked storage: it was never held
  }
  return found;
}
