import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import type { Item } from "@/api/board";
import type { Change, FeedState } from "../feed";
import { useBoardStore } from "./board";
import { useFeedStore } from "./feed";

const feed = vi.hoisted(() => ({ opts: undefined as { onChange(c: Change): void; onState(s: FeedState): void } | undefined }));
vi.mock("../feed", () => ({
  connectFeed: (opts: { onChange(c: Change): void; onState(s: FeedState): void }) => {
    feed.opts = opts;
    return () => {};
  },
}));

const api = vi.hoisted(() => ({
  listItems: vi.fn(),
  createItem: vi.fn(),
  patchItem: vi.fn(),
  moveItem: vi.fn(),
  addComment: vi.fn(),
  editComment: vi.fn(),
}));
vi.mock("@/api/board", () => api);

function item(key: string, updated_at: number, status: Item["status"] = "todo"): Item {
  return {
    key,
    level: "story",
    title: key,
    description: "",
    status,
    parent: null,
    labels: [],
    comments: [],
    created_at: 1,
    updated_at,
    created_by: "noah",
  };
}

/** What the server holds; `listItems` answers with a copy of it. */
let server: Item[];
const flush = async () => {
  for (let i = 0; i < 4; i++) await nextTick();
};
const change = (key: string | null) => feed.opts?.onChange({ topic: "board", key });

beforeEach(async () => {
  vi.useFakeTimers();
  server = [item("AIR-8", 10), item("AIR-17", 10)];
  api.listItems.mockReset().mockImplementation(async () => structuredClone(server));
  for (const fn of [api.createItem, api.patchItem, api.moveItem, api.addComment, api.editComment]) fn.mockReset();
  setActivePinia(createPinia());
  useFeedStore().start();
});

afterEach(() => {
  vi.useRealTimers();
});

test("the store holds the board's items and nothing rings on the first load", async () => {
  const board = useBoardStore();
  expect(board.items).toEqual([]);
  await flush();

  expect(board.items.map((i) => i.key)).toEqual(["AIR-8", "AIR-17"]);
  expect(board.changed.size).toBe(0);
});

test("only someone else's change rings a card, and the ring fades after 4 s", async () => {
  const board = useBoardStore();
  await flush();

  // this tab moves AIR-17; the server publishes, the page refetches, and the watcher echoes without a key
  api.moveItem.mockImplementation(async () => {
    server = [item("AIR-8", 10), item("AIR-17", 20, "doing")];
    change("AIR-17"); // the refetch can land before the POST answers
    return structuredClone(server[1]);
  });
  await board.move("AIR-17", "doing");
  await flush();
  change(null);
  await flush();
  expect(board.items.find((i) => i.key === "AIR-17")?.status).toBe("doing");
  expect(board.changed.has("AIR-17")).toBe(false);

  // someone else changes AIR-8
  server = [item("AIR-8", 30), item("AIR-17", 20, "doing")];
  change("AIR-8");
  await flush();
  expect(board.changed.has("AIR-8")).toBe(true);
  expect(board.changed.has("AIR-17")).toBe(false);

  vi.advanceTimersByTime(3999);
  expect(board.changed.has("AIR-8")).toBe(true);
  vi.advanceTimersByTime(1);
  expect(board.changed.has("AIR-8")).toBe(false);
});

test("someone else's second change rings again and restarts the 4 s", async () => {
  const board = useBoardStore();
  await flush();

  server = [item("AIR-8", 30), item("AIR-17", 10)];
  change("AIR-8");
  await flush();
  vi.advanceTimersByTime(3000);
  server = [item("AIR-8", 40), item("AIR-17", 10)];
  change("AIR-8");
  await flush();
  vi.advanceTimersByTime(3000);
  expect(board.changed.has("AIR-8")).toBe(true);
  vi.advanceTimersByTime(1000);
  expect(board.changed.has("AIR-8")).toBe(false);
});

test("a change by someone else after this tab's own write still rings", async () => {
  const board = useBoardStore();
  await flush();
  api.patchItem.mockImplementation(async () => {
    server = [item("AIR-8", 20), item("AIR-17", 10)];
    return structuredClone(server[0]);
  });
  await board.patch("AIR-8", { title: "mine" });
  await flush();
  expect(board.changed.size).toBe(0);

  server = [item("AIR-8", 25), item("AIR-17", 10)];
  change("AIR-8");
  await flush();

  expect(board.changed.has("AIR-8")).toBe(true);
});

test("an item someone else adds rings; one this tab creates does not; a removed one does not", async () => {
  const board = useBoardStore();
  await flush();

  api.createItem.mockImplementation(async () => {
    server = [...server, item("AIR-18", 20)];
    change("AIR-18");
    return structuredClone(server[2]);
  });
  await board.create({ level: "story", title: "mine" });
  await flush();
  expect(board.changed.size).toBe(0);

  server = [...server, item("AIR-19", 30)];
  change("AIR-19");
  await flush();
  expect([...board.changed]).toEqual(["AIR-19"]);

  server = server.filter((i) => i.key !== "AIR-8");
  change(null);
  await flush();
  expect([...board.changed]).toEqual(["AIR-19"]);
});

test("a comment and a comment edit are the tab's own writes too", async () => {
  const board = useBoardStore();
  await flush();
  api.addComment.mockImplementation(async () => {
    server = [item("AIR-8", 20), item("AIR-17", 10)];
    return structuredClone(server[0]);
  });
  api.editComment.mockImplementation(async () => {
    server = [item("AIR-8", 21), item("AIR-17", 10)];
    return structuredClone(server[0]);
  });

  await board.comment("AIR-8", "hi");
  await board.editComment("AIR-8", 1, "hello");
  await flush();

  expect(board.changed.size).toBe(0);
  expect(api.addComment).toHaveBeenCalledWith("AIR-8", "hi");
  expect(api.editComment).toHaveBeenCalledWith("AIR-8", 1, "hello");
});

test("a write that fails marks nothing and passes the error on", async () => {
  const board = useBoardStore();
  await flush();
  api.patchItem.mockRejectedValue(new Error("422"));

  await expect(board.patch("AIR-8", { title: "" })).rejects.toThrow("422");

  server = [item("AIR-8", 30), item("AIR-17", 10)];
  change("AIR-8");
  await flush();
  expect(board.changed.has("AIR-8")).toBe(true);
});

test("a failed load keeps the items and reports the error", async () => {
  const board = useBoardStore();
  await flush();
  api.listItems.mockRejectedValueOnce(new Error("offline"));

  change(null);
  await flush();

  expect(board.items).toHaveLength(2);
  expect(board.error?.message).toBe("offline");
});
