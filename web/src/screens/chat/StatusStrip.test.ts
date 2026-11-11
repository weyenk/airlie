import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import type { Change, FeedState } from "../../feed";
import { useFeedStore } from "../../stores/feed";
import StatusStrip from "./StatusStrip.vue";

const feed = vi.hoisted(() => ({ opts: undefined as { onChange(c: Change): void; onState(s: FeedState): void } | undefined }));
vi.mock("../../feed", () => ({
  connectFeed: (opts: { onChange(c: Change): void; onState(s: FeedState): void }) => {
    feed.opts = opts;
    return () => {};
  },
}));
const api = vi.hoisted(() => ({ listRunning: vi.fn(), listStories: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));
const engines = vi.hoisted(() => ({ listEngines: vi.fn() }));
vi.mock("@/api/engines", () => engines);
const http = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api")>()), ...http }));

const running = (n: number) => Array.from({ length: n }, (_, i) => ({ kind: "story", key: `AIR-${i}` }));
const blocked = (n: number) => ({ stories: [], total: n, page: 1, size: 1 });

async function open() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });
  const w = mount(StatusStrip, { global: { plugins: [router] } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.useFakeTimers();
  setActivePinia(createPinia());
  useFeedStore().start();
  api.listRunning.mockReset().mockResolvedValue(running(2));
  api.listStories.mockReset().mockResolvedValue(blocked(1));
  engines.listEngines.mockReset().mockResolvedValue([]);
  http.request.mockReset().mockResolvedValue({ data: [] });
});
afterEach(() => vi.useRealTimers());

test("it shows what needs you, what is running and that Studio is online, with their links", async () => {
  const w = await open();
  expect(w.get("[data-strip=needs]").text()).toBe("1 needs you");
  expect(w.get("[data-strip=needs]").attributes("href")).toBe("/workflows?blocked=true");
  expect(w.get("[data-strip=running]").text()).toBe("2 running");
  expect(w.get("[data-strip=running]").attributes("href")).toBe("/workflows");
  expect(w.get("[data-strip=studio]").text()).toBe("Studio online");
  expect(api.listStories).toHaveBeenCalledWith({ blocked: true, size: 1 });
  expect(http.request).toHaveBeenCalledWith("/v1/models");
});

test("the counts follow the running list and the blocked stories as the feed changes them", async () => {
  const w = await open();
  api.listRunning.mockResolvedValue(running(3));
  api.listStories.mockResolvedValue(blocked(2));
  feed.opts!.onChange({ topic: "stories", key: null });
  await flushPromises();
  expect(w.get("[data-strip=running]").text()).toBe("3 running");
  expect(w.get("[data-strip=needs]").text()).toBe("2 need you");

  api.listRunning.mockResolvedValue([]);
  api.listStories.mockResolvedValue(blocked(0));
  feed.opts!.onChange({ topic: "stories", key: null });
  await flushPromises();
  expect(w.get("[data-strip=running]").text()).toBe("0 running");
  expect(w.find("[data-strip=needs]").exists()).toBe(false);
});

test("Studio goes offline when the engine does not answer, and online again when it does, every 15 seconds", async () => {
  const w = await open();
  http.request.mockRejectedValue(new ApiError(502, "down"));
  await vi.advanceTimersByTimeAsync(15000);
  expect(w.get("[data-strip=studio]").text()).toBe("Studio offline");
  http.request.mockResolvedValue({ data: [] });
  await vi.advanceTimersByTimeAsync(15000);
  expect(w.get("[data-strip=studio]").text()).toBe("Studio online");
});

const gemma = (over: object) => ({
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

test("it says what the chat's engine is doing while it is not ready", async () => {
  engines.listEngines.mockResolvedValue([
    gemma({ state: "downloading", progress: { done_bytes: 7.2e9, total_bytes: 19.4e9, rate_bps: 1e7, eta_s: 180 } }),
  ]);

  const w = await open();

  expect(w.get("[role=status]").text()).toBe("Downloading gemma: 7.2 of 19.4 GB, 3 min left");
  expect(w.find("[role=progressbar]").exists()).toBe(true);
});

test("it follows the chat's engine as the feed changes it, and says nothing once it is ready", async () => {
  engines.listEngines.mockResolvedValue([gemma({ state: "starting" })]);
  const w = await open();
  expect(w.get("[role=status]").text()).toBe("Starting gemma");

  engines.listEngines.mockResolvedValue([gemma({ state: "ready" })]);
  feed.opts!.onChange({ topic: "engines", key: "gemma" });
  await flushPromises();

  expect(w.find("[role=status]").exists()).toBe(false);
});

test("it says nothing about an engine that serves another role", async () => {
  engines.listEngines.mockResolvedValue([gemma({ name: "reviewer", state: "starting", roles: ["reviewer"] })]);

  const w = await open();

  expect(w.find("[role=status]").exists()).toBe(false);
});

test("it stops asking when it is gone", async () => {
  const w = await open();
  w.unmount();
  http.request.mockClear();
  await vi.advanceTimersByTimeAsync(60000);
  expect(http.request).not.toHaveBeenCalled();
});

test("a slow answer from an earlier question cannot overwrite a newer one", async () => {
  const w = await open();
  let slow!: (v: unknown) => void;
  http.request.mockImplementationOnce(() => new Promise((r) => (slow = r)));
  await vi.advanceTimersByTimeAsync(15000); // asks: pending
  http.request.mockRejectedValueOnce(new ApiError(502, "down"));
  await vi.advanceTimersByTimeAsync(15000); // asks again: offline
  expect(w.get("[data-strip=studio]").text()).toBe("Studio offline");
  slow({ data: [] }); // the first question answers last
  await flushPromises();
  expect(w.get("[data-strip=studio]").text()).toBe("Studio offline");
});
