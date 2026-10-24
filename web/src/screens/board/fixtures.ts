import { createMemoryHistory, createRouter } from "vue-router";
import type { Item } from "@/api/board";

/** A board item for the tests, with the fields a test does not care about filled in. */
export function item(key: string, extra: Partial<Item> = {}): Item {
  return {
    key,
    level: "story",
    title: `Title of ${key}`,
    description: "",
    status: "todo",
    parent: null,
    labels: [],
    comments: [],
    created_at: 1,
    updated_at: 1,
    created_by: "noah",
    ...extra,
  };
}

/** The plugins a card needs: a router that knows the board's address. */
export function boardRouter() {
  return createRouter({ history: createMemoryHistory(), routes: [{ path: "/board/:key?", name: "board", component: { template: "<i/>" } }] });
}
