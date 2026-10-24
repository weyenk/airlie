import { defineStore } from "pinia";
import { computed, getCurrentScope, onScopeDispose, ref, watch } from "vue";
import * as api from "@/api/board";
import type { Item, ItemChange, NewItem, Status } from "@/api/board";
import { liveResource } from "./live";

/** How long a card shows "updated just now" after someone else changed it. */
export const RING_MS = 4000;

/**
 * The board's items, current through the live feed, and `changed`: the keys someone else changed in the last
 * 4 s. A refetch is compared with the list before it, by key and `updated_at`. This tab's own writes must not
 * ring (the watcher echoes them with no key, so the refetch cannot tell whose they were), so each write is
 * remembered: while it is in flight its key is held back, and once it answers, any `updated_at` up to the one it
 * returned is the tab's own. A change after that, by anyone else, rings.
 */
export const useBoardStore = defineStore("board", () => {
  const board = liveResource("board", api.listItems);
  const items = computed<Item[]>(() => board.data.value ?? []);
  const changed = ref<Set<string>>(new Set());

  const inFlight = new Map<string, number>(); // key: writes begun and not answered
  const ownUpdatedAt = new Map<string, number>(); // key: the newest `updated_at` this tab's writes returned
  let creating = 0; // creates in flight: their key is not known until they answer
  const timers = new Map<string, ReturnType<typeof setTimeout>>();

  function ring(key: string): void {
    changed.value = new Set(changed.value).add(key);
    clearTimeout(timers.get(key));
    timers.set(
      key,
      setTimeout(() => {
        timers.delete(key);
        const next = new Set(changed.value);
        next.delete(key);
        changed.value = next;
      }, RING_MS),
    );
  }

  const isOwn = (item: Item, isNew: boolean): boolean =>
    (inFlight.get(item.key) ?? 0) > 0 ||
    (ownUpdatedAt.get(item.key) ?? -Infinity) >= item.updated_at ||
    (isNew && creating > 0);

  // sync, so the comparison sees the list exactly as it was replaced, never two loads merged
  watch(
    board.data,
    (next, previous) => {
      if (!next || !previous) return; // the first load rings nothing
      const before = new Map(previous.map((i) => [i.key, i]));
      for (const item of next) {
        const was = before.get(item.key);
        if (was?.updated_at === item.updated_at) continue;
        if (!isOwn(item, was === undefined)) ring(item.key);
      }
    },
    { flush: "sync" },
  );

  if (getCurrentScope()) onScopeDispose(() => timers.forEach(clearTimeout));

  /** Run a write of `key`'s, remembering it as this tab's own, and take the list back after it. */
  async function write(key: string | null, run: () => Promise<Item>): Promise<Item> {
    if (key === null) creating++;
    else inFlight.set(key, (inFlight.get(key) ?? 0) + 1);
    try {
      const done = await run();
      ownUpdatedAt.set(done.key, Math.max(ownUpdatedAt.get(done.key) ?? -Infinity, done.updated_at));
      return done;
    } finally {
      if (key === null) creating--;
      else if ((inFlight.get(key) ?? 1) <= 1) inFlight.delete(key);
      else inFlight.set(key, inFlight.get(key)! - 1);
      void board.refresh();
    }
  }

  const create = (body: NewItem) => write(null, () => api.createItem(body));
  const patch = (key: string, body: ItemChange) => write(key, () => api.patchItem(key, body));
  const move = (key: string, status: Status, before: string | null = null) => write(key, () => api.moveItem(key, status, before));
  const comment = (key: string, text: string) => write(key, () => api.addComment(key, text));
  const editComment = (key: string, id: number, text: string) => write(key, () => api.editComment(key, id, text));

  return { items, changed, error: board.error, refresh: board.refresh, create, patch, move, comment, editComment };
});
