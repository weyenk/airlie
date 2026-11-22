import { defineStore } from "pinia";
import { reactive, ref } from "vue";
import { chatOptions, sessionStatus, updateSession } from "@/api/sessions";
import type { ChatOptions } from "@/api/sessions";

type Info = { label: string; askFirst: string[]; model: string; persona: string };

/** What a chat's status route says that config decides (the model's name, what it asks before using, the model and persona it runs
 * with): read once per chat, so the header and the composer's footer both have it without each asking. A failed read leaves it
 * unknown (no label shown). The models and personas a chat can be set to are read once for all chats. */
export const useSessionInfo = defineStore("sessionInfo", () => {
  const byId = reactive<Record<string, Info>>({});
  const options = ref<ChatOptions>({ models: [], personas: [] });
  const asked = new Set<string>();
  let optionsAsked = false;

  function load(id: string): void {
    if (asked.has(id)) return;
    asked.add(id);
    sessionStatus(id).then(
      (s) => {
        byId[id] = { label: s.engine.label, askFirst: s.ask_first, model: s.model, persona: s.persona };
      },
      () => asked.delete(id), // asked again the next time something wants it
    );
    loadOptions();
  }

  function loadOptions(): void {
    if (optionsAsked) return;
    optionsAsked = true;
    chatOptions().then(
      (o) => {
        options.value = o;
      },
      () => {
        optionsAsked = false; // asked again with the next chat
      },
    );
  }

  /** Change the chat's model and/or persona (only what changed is sent); rejects with the server's reason when it refuses. */
  async function choose(id: string, settings: { model?: string; persona?: string }): Promise<void> {
    const row = await updateSession(id, settings);
    const held = byId[id];
    if (!held) return;
    held.model = row.model;
    held.persona = row.persona;
    held.label = options.value.models.find((m) => m.id === row.model)?.label ?? row.model;
  }

  return {
    load,
    choose,
    options,
    label: (id: string): string | null => byId[id]?.label ?? null,
    askFirst: (id: string): string[] | null => byId[id]?.askFirst ?? null,
    model: (id: string): string | null => byId[id]?.model ?? null,
    persona: (id: string): string | null => byId[id]?.persona ?? null,
  };
});
