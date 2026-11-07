import { defineStore } from "pinia";
import { ref } from "vue";
import type { Level } from "@/api/board";

/** What Create had typed when the person left for the builder. */
export type CreateForm = { level: Level; title: string; description: string; parent: string | null; repo: string; workflow: string };

const STORAGE = "airlie.cardDraft";

function load(): Record<string, CreateForm> {
  try {
    const raw = sessionStorage.getItem(STORAGE);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? (parsed as Record<string, CreateForm>) : {};
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

/**
 * The half-written cards held while the person is in the builder, by token. The builder sets the workflow it
 * made (`setWorkflow`) and comes back with the token; Create restores the form from it (`take`) and forgets it
 * once the card is made (`drop`). Kept in sessionStorage, so a reload mid-trip loses nothing.
 */
export const useCardDraftStore = defineStore("cardDraft", () => {
  const forms = ref<Record<string, CreateForm>>(load());

  function save(): void {
    try {
      sessionStorage.setItem(STORAGE, JSON.stringify(forms.value));
    } catch {
      // blocked storage: held for this page load only
    }
  }

  function hold(form: CreateForm): string {
    const token = newToken();
    forms.value = { ...forms.value, [token]: { ...form } };
    save();
    return token;
  }

  function take(token: string): CreateForm | null {
    const form = forms.value[token];
    return form ? { ...form } : null;
  }

  function drop(token: string): void {
    if (!(token in forms.value)) return;
    const rest = { ...forms.value };
    delete rest[token];
    forms.value = rest;
    save();
  }

  function setWorkflow(token: string, name: string): void {
    const form = forms.value[token];
    if (!form) return;
    forms.value = { ...forms.value, [token]: { ...form, workflow: name } };
    save();
  }

  return { forms, hold, take, drop, setWorkflow };
});
