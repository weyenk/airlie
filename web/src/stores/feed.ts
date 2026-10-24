import { defineStore } from "pinia";
import { ref } from "vue";
import { connectFeed } from "../feed";
import { useSessionStore } from "./session";
import type { FeedState } from "../feed";

/** The page's one live feed: its state, and the changes it hears handed to whoever listens by topic. */
export const useFeedStore = defineStore("feed", () => {
  const state = ref<FeedState>("connecting");
  /** One more on every `live`: the feed has no replay, so a change in this number means "refetch what you show". */
  const generation = ref(0);
  const listeners = new Map<string, Set<(key: string | null) => void>>();
  let stopFeed: (() => void) | undefined;

  /** `fn` hears the key of every change to `topic` (null: the topic changed as a whole). Returns the unsubscribe. */
  function on(topic: string, fn: (key: string | null) => void): () => void {
    let set = listeners.get(topic);
    if (!set) listeners.set(topic, (set = new Set()));
    set.add(fn);
    return () => {
      set.delete(fn);
    };
  }

  function start(): void {
    if (stopFeed) return;
    stopFeed = connectFeed({
      onChange: ({ topic, key }) => {
        // a copy, so a listener that unsubscribes mid-way does not skip the next one; and one that throws does
        // not starve the rest
        for (const fn of [...(listeners.get(topic) ?? [])]) {
          try {
            fn(key);
          } catch (e) {
            console.error(`feed: a ${topic} listener threw`, e);
          }
        }
      },
      onUnauthorized: () => useSessionStore().signedOut(),
      onState: (next) => {
        state.value = next;
        if (next === "live") generation.value += 1;
      },
    });
  }

  /** Close the feed (signed out, or on a public page); `start` opens it again. */
  function stop(): void {
    stopFeed?.();
    stopFeed = undefined;
    state.value = "connecting";
  }

  return { state, generation, on, start, stop };
});
