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
