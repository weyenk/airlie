import { defineStore } from "pinia";
import { computed } from "vue";
import { listPins, pinRepository, unpinRepository } from "@/api/sessions";
import { liveResource } from "./live";

/** The repositories this person pinned, in pin order, current through the live feed on the `chat` topic. */
export const usePins = defineStore("pins", () => {
  const live = liveResource("chat", listPins);
  const pinned = computed<string[]>(() => live.data.value ?? []);

  /** A refused pin (a name airlie does not list, a 51st pin) rejects with the server's reason. */
  async function pin(repo: string): Promise<void> {
    await pinRepository(repo);
    await live.refresh();
  }

  async function unpin(repo: string): Promise<void> {
    await unpinRepository(repo);
    await live.refresh();
  }

  return { pinned, error: live.error, refresh: live.refresh, pin, unpin };
});
