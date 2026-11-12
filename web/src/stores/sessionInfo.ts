import { defineStore } from "pinia";
import { reactive } from "vue";
import { sessionStatus } from "@/api/sessions";

type Info = { label: string; askFirst: string[] };

/** What a chat's status route says that config decides (the model's name, what it asks before using): read once per chat, so
 * the header and the composer's footer both have it without each asking. A failed read leaves it unknown (no label shown). */
export const useSessionInfo = defineStore("sessionInfo", () => {
  const byId = reactive<Record<string, Info>>({});
  const asked = new Set<string>();

  function load(id: string): void {
    if (asked.has(id)) return;
    asked.add(id);
    sessionStatus(id).then(
      (s) => {
        byId[id] = { label: s.engine.label, askFirst: s.ask_first };
      },
      () => asked.delete(id), // asked again the next time something wants it
    );
  }

  return {
    load,
    label: (id: string): string | null => byId[id]?.label ?? null,
    askFirst: (id: string): string[] | null => byId[id]?.askFirst ?? null,
  };
});
