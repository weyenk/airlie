import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import type { Change, FeedState } from "../feed";
import { useFeedStore } from "./feed";
import { usePeopleStore } from "./people";

const feed = vi.hoisted(() => ({ opts: undefined as { onChange(c: Change): void; onState(s: FeedState): void } | undefined }));
vi.mock("../feed", () => ({
  connectFeed: (opts: { onChange(c: Change): void; onState(s: FeedState): void }) => {
    feed.opts = opts;
    return () => {};
  },
}));

const api = vi.hoisted(() => ({ listUsers: vi.fn() }));
vi.mock("@/api/auth", () => api);

const flush = async () => {
  for (let i = 0; i < 4; i++) await nextTick();
};

beforeEach(() => {
  api.listUsers.mockReset().mockResolvedValue([{ id: "u1", name: "Ann" }]);
  setActivePinia(createPinia());
  useFeedStore().start();
});

test("users load and follow the users topic", async () => {
  const store = usePeopleStore();
  expect(store.users).toEqual([]);
  await flush();
  expect(store.users).toEqual([{ id: "u1", name: "Ann" }]);

  api.listUsers.mockResolvedValue([{ id: "u1", name: "Ann" }, { id: "u2", name: "Bo" }]);
  feed.opts?.onChange({ topic: "users", key: null });
  await flush();

  expect(store.users.map((u) => u.id)).toEqual(["u1", "u2"]);
});

test("a non-admin's 403 leaves no users and sets the error", async () => {
  api.listUsers.mockRejectedValue(new Error("admin only"));
  const store = usePeopleStore();
  await flush();

  expect(store.users).toEqual([]);
  expect(store.error?.message).toBe("admin only");
});
