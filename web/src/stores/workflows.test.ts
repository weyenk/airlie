import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import type { Change, FeedState } from "../feed";
import { useFeedStore } from "./feed";
import { useWorkflowsStore } from "./workflows";

const feed = vi.hoisted(() => ({ opts: undefined as { onChange(c: Change): void; onState(s: FeedState): void } | undefined }));
vi.mock("../feed", () => ({
  connectFeed: (opts: { onChange(c: Change): void; onState(s: FeedState): void }) => {
    feed.opts = opts;
    return () => {};
  },
}));

const api = vi.hoisted(() => ({ listRunning: vi.fn(), listStories: vi.fn() }));
vi.mock("@/api/workflows", () => api);

const flush = async () => {
  for (let i = 0; i < 4; i++) await nextTick();
};
const change = (topic: string, key: string | null) => feed.opts?.onChange({ topic, key });
const page = (keys: string[]) => ({ stories: keys.map((key) => ({ key })), total: keys.length, page: 1, size: null });

beforeEach(() => {
  api.listRunning.mockReset().mockResolvedValue([{ kind: "story", key: "AIR-1" }]);
  api.listStories.mockReset().mockResolvedValue(page(["AIR-1", "AIR-2"]));
  setActivePinia(createPinia());
  useFeedStore().start();
});

test("running refetches on stories, runs and jobs changes, and on no other topic", async () => {
  const store = useWorkflowsStore();
  await flush();
  expect(store.running).toEqual([{ kind: "story", key: "AIR-1" }]);
  expect(api.listRunning).toHaveBeenCalledTimes(1);

  for (const topic of ["stories", "runs", "jobs"]) {
    change(topic, null);
    await flush();
  }
  expect(api.listRunning).toHaveBeenCalledTimes(4);

  change("board", null);
  change("users", null);
  await flush();
  expect(api.listRunning).toHaveBeenCalledTimes(4);
});

test("the stories page loads with its query, and a new query loads again", async () => {
  const store = useWorkflowsStore();
  await flush();
  expect(store.stories?.total).toBe(2);
  expect(api.listStories).toHaveBeenLastCalledWith({});

  store.query = { page: 2, size: 10, stage: "build" };
  await flush();

  expect(api.listStories).toHaveBeenCalledTimes(2);
  expect(api.listStories).toHaveBeenLastCalledWith({ page: 2, size: 10, stage: "build" });
});

test("the stories page refetches when a story changes", async () => {
  useWorkflowsStore();
  await flush();

  change("stories", "AIR-1");
  await flush();

  expect(api.listStories).toHaveBeenCalledTimes(2);
});

test("a failed stories load keeps the last page and reports the error", async () => {
  const store = useWorkflowsStore();
  await flush();
  api.listStories.mockRejectedValueOnce(new Error("offline"));

  change("stories", null);
  await flush();

  expect(store.stories?.total).toBe(2);
  expect(store.storiesError?.message).toBe("offline");
});
