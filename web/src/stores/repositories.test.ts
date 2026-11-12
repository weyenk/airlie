import { createPinia, setActivePinia } from "pinia";
import { effectScope, nextTick } from "vue";
import type { PersonRepos } from "@/api/sources";
import { useRepositories } from "./repositories";
import type { Change, FeedState } from "../feed";
import { useFeedStore } from "./feed";

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

const VIEW: PersonRepos = {
  sources: [
    {
      name: "github_app",
      title: "GitHub",
      linked: true,
      login: "weyenk",
      repositories: [{ repo: "weyenk/airlie", private: true, push: true, url: "https://github.com/weyenk/airlie" }],
      error: null,
      listed_at: 1,
    },
  ],
  extras: ["local/only"],
  extra_candidates: ["local/only", "other/local"],
};

const flush = async () => {
  await nextTick();
  await nextTick();
};

beforeEach(() => {
  feed.opts = undefined;
  setActivePinia(createPinia());
  useFeedStore().start();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/v1/events" ? new Promise<Response>(() => {}) : url.startsWith("/v1/airlie/chat/repositories") ? Response.json(VIEW) : Response.json({}),
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("shown lists source repositories then extras", async () => {
  const repos = useRepositories();
  await repos.reload();
  await flush();
  expect(repos.shown).toEqual(["weyenk/airlie", "local/only"]);
});

test("refresh asks the server with refresh=true", async () => {
  const repos = useRepositories();
  await flush();
  const fetchMock = vi.mocked(fetch);
  fetchMock.mockClear();
  await repos.refresh();
  expect(fetchMock).toHaveBeenCalledWith("/v1/airlie/chat/repositories?refresh=true", expect.any(Object));
});

test("a chat hint refetches the listing", async () => {
  const scope = effectScope();
  scope.run(() => useRepositories());
  await flush();
  const fetchMock = vi.mocked(fetch);
  const before = fetchMock.mock.calls.length;
  feed.opts?.onChange({ topic: "chat", key: null });
  await flush();
  expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
});
