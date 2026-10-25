import { getCurrentScope, onScopeDispose, shallowRef, watch } from "vue";
import type { Ref } from "vue";
import { useFeedStore } from "./feed";

export type LiveResource<T> = {
  data: Ref<T | undefined>;
  error: Ref<Error | undefined>;
  refresh(): Promise<void>;
};

/**
 * Data from `load` that stays current: it loads at once, refetches when `topic` (or any of several) changes (an event whose key is
 * null, or `key`; any key when none is given), and refetches whenever the feed (re)connects, since changes
 * between are not replayed. A failed load keeps the data it had and sets `error`; `refresh` never rejects.
 * Inside a component or effect scope it stops listening when the scope ends.
 */
export function liveResource<T>(topic: string | string[], load: () => Promise<T>, key?: string): LiveResource<T> {
  const feed = useFeedStore();
  // each load replaces the data whole, so there is nothing for deep reactivity to watch
  const data = shallowRef<T>();
  const error = shallowRef<Error>();
  let latest = 0;

  async function refresh(): Promise<void> {
    const mine = ++latest;
    try {
      const value = await load();
      if (mine !== latest) return; // a newer load has begun; its answer wins
      data.value = value;
      error.value = undefined;
    } catch (e) {
      if (mine !== latest) return;
      error.value = e instanceof Error ? e : new Error(String(e));
    }
  }

  const offs = [topic].flat().map((t) =>
    feed.on(t, (changed) => {
      if (changed === null || key === undefined || changed === key) void refresh();
    }),
  );
  const stopWatching = watch(
    () => feed.generation,
    () => void refresh(),
  );
  if (getCurrentScope()) {
    onScopeDispose(() => {
      for (const off of offs) off();
      stopWatching();
    });
  }

  void refresh();
  return { data, error, refresh };
}
