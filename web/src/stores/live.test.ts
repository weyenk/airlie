import { createPinia, setActivePinia } from "pinia";
import { effectScope, nextTick } from "vue";
import type { Change, FeedState } from "../feed";
import { useFeedStore } from "./feed";
import { liveResource } from "./live";

// The page's feed is the one seam faked here: the test is the server, and calls onChange and onState itself.
const feed = vi.hoisted(() => ({
  opts: undefined as { onChange(c: Change): void; onState(s: FeedState): void } | undefined,
  stop: vi.fn(),
  connectFeed: vi.fn(),
}));
vi.mock("../feed", () => ({
  connectFeed: (opts: { onChange(c: Change): void; onState(s: FeedState): void }) => {
    feed.opts = opts;
    feed.connectFeed(opts);
    return feed.stop;
  },
}));

const change = (topic: string, key: string | null) => feed.opts?.onChange({ topic, key });
const state = (s: FeedState) => feed.opts?.onState(s);
/** Let the pending loads and watchers run. */
const flush = async () => {
  await nextTick();
  await nextTick();
};

beforeEach(() => {
  feed.opts = undefined;
  feed.stop.mockReset();
  feed.connectFeed.mockReset();
  setActivePinia(createPinia());
  useFeedStore().start();
});

afterEach(() => {
  vi.restoreAllMocks();
});

test("a live resource loads at once and holds the data", async () => {
  const load = vi.fn(async () => ["a", "b"]);

  const board = liveResource("board", load);
  expect(board.data.value).toBeUndefined();
  await flush();

  expect(load).toHaveBeenCalledTimes(1);
  expect(board.data.value).toEqual(["a", "b"]);
  expect(board.error.value).toBeUndefined();
});

test("a live resource refetches on its topic and key, not on others", async () => {
  const load = vi.fn(async () => "x");
  liveResource("board", load, "AIR-1");
  await flush();
  expect(load).toHaveBeenCalledTimes(1);

  change("board", "AIR-2");
  await flush();
  expect(load).toHaveBeenCalledTimes(1);

  change("board", "AIR-1");
  await flush();
  expect(load).toHaveBeenCalledTimes(2);

  change("board", null);
  await flush();
  expect(load).toHaveBeenCalledTimes(3);

  change("runs", null);
  change("runs", "AIR-1");
  await flush();
  expect(load).toHaveBeenCalledTimes(3);
});

test("a resource with no key refetches on any change to its topic", async () => {
  const load = vi.fn(async () => "x");
  liveResource("stories", load);
  await flush();

  change("stories", "AIR-9");
  change("stories", null);
  change("board", "AIR-9");
  await flush();

  expect(load).toHaveBeenCalledTimes(3);
});

test("a reconnect refetches every live resource", async () => {
  const loadBoard = vi.fn(async () => "board");
  const loadRuns = vi.fn(async () => "runs");
  liveResource("board", loadBoard);
  liveResource("runs", loadRuns);
  const store = useFeedStore();
  await flush();
  expect([loadBoard.mock.calls.length, loadRuns.mock.calls.length]).toEqual([1, 1]);

  state("live");
  await flush();
  expect(store.generation).toBe(1);
  expect([loadBoard.mock.calls.length, loadRuns.mock.calls.length]).toEqual([2, 2]);

  state("reconnecting");
  await flush();
  expect(store.generation).toBe(1);
  expect([loadBoard.mock.calls.length, loadRuns.mock.calls.length]).toEqual([2, 2]);

  state("live");
  await flush();
  expect(store.generation).toBe(2);
  expect([loadBoard.mock.calls.length, loadRuns.mock.calls.length]).toEqual([3, 3]);
});

test("a failed load sets error and keeps the data it had; the next good load clears it", async () => {
  const load = vi
    .fn<() => Promise<string>>()
    .mockResolvedValueOnce("first")
    .mockRejectedValueOnce(new Error("boom"))
    .mockResolvedValueOnce("third");
  const board = liveResource("board", load);
  await flush();
  expect(board.data.value).toBe("first");

  change("board", null);
  await flush();
  expect(board.data.value).toBe("first");
  expect(board.error.value).toEqual(new Error("boom"));

  await board.refresh();
  expect(board.data.value).toBe("third");
  expect(board.error.value).toBeUndefined();
});

test("a thrown value that is not an Error is wrapped", async () => {
  const board = liveResource("board", () => Promise.reject("plain"));
  await flush();

  expect(board.error.value).toBeInstanceOf(Error);
  expect(board.error.value?.message).toBe("plain");
});

test("a slow older load does not overwrite a newer one", async () => {
  const resolvers: Array<(v: string) => void> = [];
  const load = vi.fn(() => new Promise<string>((resolve) => resolvers.push(resolve)));
  const board = liveResource("board", load);
  change("board", null);
  expect(load).toHaveBeenCalledTimes(2);

  resolvers[1]("new");
  await flush();
  resolvers[0]("old");
  await flush();

  expect(board.data.value).toBe("new");
});

test("refresh resolves once the data is in", async () => {
  let n = 0;
  const board = liveResource("board", async () => ++n);
  await board.refresh();

  expect(board.data.value).toBe(2);
});

test("a resource stops listening when its scope ends", async () => {
  const load = vi.fn(async () => "x");
  const scope = effectScope();
  scope.run(() => liveResource("board", load));
  await flush();

  scope.stop();
  change("board", null);
  state("live");
  await flush();

  expect(load).toHaveBeenCalledTimes(1);
});

test("the feed store starts the feed once and reports its state", () => {
  const store = useFeedStore();
  store.start();
  store.start();

  expect(feed.connectFeed).toHaveBeenCalledTimes(1);
  expect(store.state).toBe("connecting");
  state("live");
  expect(store.state).toBe("live");
  state("reconnecting");
  expect(store.state).toBe("reconnecting");
});

test("on() hands a topic's changes to its listeners until they unsubscribe", () => {
  const store = useFeedStore();
  const board = vi.fn();
  const runs = vi.fn();
  const off = store.on("board", board);
  store.on("runs", runs);

  change("board", "AIR-1");
  change("runs", null);
  off();
  change("board", "AIR-2");

  expect(board.mock.calls).toEqual([["AIR-1"]]);
  expect(runs.mock.calls).toEqual([[null]]);
});

test("a listener that throws does not starve the others", () => {
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  const store = useFeedStore();
  const second = vi.fn();
  store.on("board", () => {
    throw new Error("bad listener");
  });
  store.on("board", second);

  change("board", null);

  expect(second).toHaveBeenCalledTimes(1);
  expect(error).toHaveBeenCalledTimes(1);
});
