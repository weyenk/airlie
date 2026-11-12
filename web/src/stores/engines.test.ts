import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import type { EngineStatus } from "@/api/engines";
import type { Change, FeedState } from "../feed";
import { useEngines } from "./engines";
import { useFeedStore } from "./feed";

// The page's feed is the one seam faked here (the test is the server); the network is a stubbed fetch.
const feed = vi.hoisted(() => ({ opts: undefined as { onChange(c: Change): void; onState(s: FeedState): void } | undefined }));
vi.mock("../feed", () => ({
  connectFeed: (opts: { onChange(c: Change): void; onState(s: FeedState): void }) => {
    feed.opts = opts;
    return () => {};
  },
}));

const engine = (over: Partial<EngineStatus>): EngineStatus => ({
  name: "gemma",
  kind: "engine",
  state: "ready",
  detail: "",
  progress: null,
  holders: [],
  lifecycle: "on_demand",
  roles: ["chat"],
  ...over,
});

/** A fetch that answers each call with the next of `answers` (a list of engines, or an Error to fail with). */
function answers(...next: (EngineStatus[] | Error)[]) {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => {
    const a = next.shift();
    if (a === undefined || a instanceof Error) return Response.json({ detail: "engines are down" }, { status: 503 });
    return Response.json(a);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  feed.opts = undefined;
  setActivePinia(createPinia());
  useFeedStore().start();
});
afterEach(() => vi.unstubAllGlobals());

test("the store lists the engines the server reports", async () => {
  answers([engine({ name: "gemma" }), engine({ name: "reviewer", roles: ["reviewer"] })]);

  const engines = useEngines();
  await flushPromises();

  expect(engines.list.map((e) => e.name)).toEqual(["gemma", "reviewer"]);
  expect(engines.byName("reviewer")?.roles).toEqual(["reviewer"]);
  expect(engines.byName("nobody")).toBeUndefined();
});

test("the store finds the engine that serves a role", async () => {
  answers([engine({ name: "gemma", roles: ["chat", "design"] }), engine({ name: "reviewer", roles: ["reviewer"] })]);

  const engines = useEngines();
  await flushPromises();

  expect(engines.forRole("chat")?.name).toBe("gemma");
  expect(engines.forRole("coder")).toBeUndefined();
});

test("an engines hint makes the store ask again and show the new state", async () => {
  const fetchMock = answers([engine({ state: "stopped" })], [engine({ state: "starting" })]);
  const engines = useEngines();
  await flushPromises();
  expect(engines.byName("gemma")?.state).toBe("stopped");

  feed.opts?.onChange({ topic: "engines", key: "gemma" });
  await flushPromises();

  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(engines.byName("gemma")?.state).toBe("starting");
});

test("a hint for another topic does not make the store ask again", async () => {
  const fetchMock = answers([engine({})]);
  useEngines();
  await flushPromises();

  feed.opts?.onChange({ topic: "board", key: "AIR-1" });
  await flushPromises();

  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("a failed refetch keeps the last list and says what went wrong", async () => {
  answers([engine({ state: "ready" })], new Error("down"));
  const engines = useEngines();
  await flushPromises();

  feed.opts?.onChange({ topic: "engines", key: "gemma" });
  await flushPromises();

  expect(engines.byName("gemma")?.state).toBe("ready");
  expect(engines.error?.message).toBe("engines are down");
});

test("before the first answer the store is not loaded, and after it is", async () => {
  answers([]);

  const engines = useEngines();
  expect(engines.loaded).toBe(false);
  await flushPromises();

  expect(engines.loaded).toBe(true);
  expect(engines.list).toEqual([]);
});
