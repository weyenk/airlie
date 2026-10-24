import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import type { StoryRow } from "@/api/workflows";
import WorkflowsScreen from "./WorkflowsScreen.vue";

const api = vi.hoisted(() => ({ listRunning: vi.fn(), listStories: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));

const STORIES: StoryRow[] = [
  { key: "AIR-17", title: "Card modal", repo: "weyenk/airlie", stage: "build", blocked: null, last_activity: 100 },
  { key: "AIR-30", title: "Architect tool call", repo: "weyenk/openhand", stage: "layers", blocked: "layer 02 failed twice", last_activity: 90 },
];

async function open(url = "/workflows") {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/workflows", name: "workflows", component: WorkflowsScreen },
      { path: "/workflows/:key", component: { template: "<i/>" } },
    ],
  });
  await router.push(url);
  const w = mount(WorkflowsScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}
const lastPageCall = () => api.listStories.mock.calls.map((c) => c[0]).filter((q) => q.size !== undefined).at(-1);

beforeEach(() => {
  setActivePinia(createPinia());
  api.listRunning.mockReset().mockResolvedValue([]);
  api.listStories.mockReset().mockImplementation(async (q: { size?: number; page?: number }) =>
    q.size === undefined
      ? { stories: STORIES, total: 2, page: 1, size: null }
      : { stories: STORIES, total: 60, page: q.page ?? 1, size: q.size },
  );
});
afterEach(() => {
  document.body.innerHTML = "";
});

test("the first page asks for 25 and shows the count", async () => {
  const { w } = await open();
  expect(lastPageCall()).toEqual({ page: 1, size: 25 });
  expect(w.get("[data-stories-count]").text()).toBe("60");
  expect(w.text()).toContain("1–25 of 60");
});

test("the address's query is what the list asks for, filter by filter", async () => {
  await open("/workflows?q=card&stage=build&repo=weyenk/airlie&blocked=true&sort=key&page=2&active_from=10&active_to=20");
  expect(lastPageCall()).toEqual({
    page: 2,
    size: 25,
    q: "card",
    stage: "build",
    repo: "weyenk/airlie",
    blocked: true,
    sort: "key",
    active_from: 10,
    active_to: 20,
  });
});

test("choosing a filter writes it to the address and goes back to page 1", async () => {
  const { w, router } = await open("/workflows?page=2");
  await w.get("[data-filter=stage]").setValue("review");
  await flushPromises();
  expect(router.currentRoute.value.query).toEqual({ stage: "review" });
  expect(lastPageCall()).toEqual({ page: 1, size: 25, stage: "review" });

  await w.get("[data-blocked=true]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.query).toEqual({ stage: "review", blocked: "true" });
  await w.get("[data-blocked=false]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.query.blocked).toBe("false");
  expect(lastPageCall()?.blocked).toBe(false);
  await w.get("[data-blocked=all]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.query.blocked).toBeUndefined();

  await w.get("[data-filter=sort]").setValue("stage");
  await flushPromises();
  expect(router.currentRoute.value.query.sort).toBe("stage");
});

test("the repository choices are the stories' own repo values, sent back verbatim", async () => {
  const { w, router } = await open();
  const options = w.get("[data-filter=repo]").findAll("option").map((o) => o.attributes("value"));
  expect(options).toEqual(["", "weyenk/airlie", "weyenk/openhand"]);
  await w.get("[data-filter=repo]").setValue("weyenk/openhand");
  await flushPromises();
  expect(router.currentRoute.value.query.repo).toBe("weyenk/openhand");
  expect(lastPageCall()).toEqual({ page: 1, size: 25, repo: "weyenk/openhand" });
});

test("search is mirrored in the address after a pause", async () => {
  vi.useFakeTimers();
  try {
    const { w, router } = await open();
    await w.get("[data-search]").setValue("card");
    expect(router.currentRoute.value.query.q).toBeUndefined();
    await vi.advanceTimersByTimeAsync(400);
    await flushPromises();
    expect(router.currentRoute.value.query.q).toBe("card");
    expect(lastPageCall()).toEqual({ page: 1, size: 25, q: "card" });
  } finally {
    vi.useRealTimers();
  }
});

test("page 2 asks for page=2&size=25, and the address keeps it", async () => {
  const { w, router } = await open();
  await w.get("[data-page='2']").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.query.page).toBe("2");
  expect(lastPageCall()).toEqual({ page: 2, size: 25 });
  expect(w.text()).toContain("26–50 of 60");
  await w.get("[data-next]").trigger("click");
  await flushPromises();
  expect(lastPageCall()?.page).toBe(3);
  expect(w.get("[data-next]").attributes("disabled")).toBeDefined();
});

test("a blocked story shows its reason; no Start button; Filters is the phone's way to the filters", async () => {
  const { w } = await open();
  expect(w.get("[data-story=AIR-30]").text()).toContain("Blocked: layer 02 failed twice");
  expect(w.text()).not.toMatch(/start/i);
  const filters = w.get("[data-filters-toggle]");
  expect(w.get("[data-filters]").classes()).toContain("hidden");
  await filters.trigger("click");
  expect(w.get("[data-filters]").classes()).not.toContain("hidden");
});

test("running rows come from the store", async () => {
  api.listRunning.mockResolvedValue([{ kind: "story", key: "AIR-17", title: "Card modal", topology: null, progress: 0.5, last_activity: 1 }]);
  const { w } = await open();
  expect(w.get("a[href='/workflows/AIR-17']").text()).toContain("Card modal");
});

test("the first request is the address's own, never an unpaged one", async () => {
  await open("/workflows?stage=build");
  const pageCalls = api.listStories.mock.calls.map((c) => c[0]).filter((q) => q.size !== undefined);
  expect(pageCalls).toEqual([{ page: 1, size: 25, stage: "build" }]);
  expect(api.listStories.mock.calls.filter((c) => c[0].size === undefined)).toHaveLength(1); // the repository choices
});

test("an invalid stage or sort is dropped from the address and is not a filter", async () => {
  const { w, router } = await open("/workflows?stage=nonsense&sort=bogus");
  expect(router.currentRoute.value.query).toEqual({});
  expect(lastPageCall()).toEqual({ page: 1, size: 25 });
  expect(w.text()).not.toContain("No stories match");
});

test("a page past the last falls back to the last page", async () => {
  const { router } = await open("/workflows?page=9");
  expect(router.currentRoute.value.query.page).toBe("3");
  expect(lastPageCall()).toEqual({ page: 3, size: 25 });
});

test("the Metrics window shows as a chip that clears both ends", async () => {
  const { w, router } = await open("/workflows?active_from=1792800000&active_to=1792886400&stage=build");
  expect(w.get("[data-window]").text()).toContain("Active");
  await w.get("[data-window] button").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.query).toEqual({ stage: "build" });
  expect(w.find("[data-window]").exists()).toBe(false);
});

test("typing keeps its trailing space while the address catches up", async () => {
  vi.useFakeTimers();
  try {
    const { w } = await open();
    const input = w.get("[data-search]");
    await input.trigger("focus");
    await input.setValue("card ");
    await vi.advanceTimersByTimeAsync(400);
    await flushPromises();
    expect((input.element as HTMLInputElement).value).toBe("card ");
  } finally {
    vi.useRealTimers();
  }
});

test("a failed repository list is shown, not swallowed", async () => {
  api.listStories.mockImplementation(async (q: { size?: number }) => {
    if (q.size === undefined) throw new Error("repos offline");
    return { stories: STORIES, total: 2, page: 1, size: q.size };
  });
  const { w } = await open();
  expect(w.text()).toContain("repos offline");
});

test("path repositories show their basename and send the full path back", async () => {
  api.listStories.mockImplementation(async (q: { size?: number }) => ({
    stories: [{ ...STORIES[0], repo: "/Users/n/git/airlie" }],
    total: 1,
    page: 1,
    size: q.size ?? null,
  }));
  const { w, router } = await open();
  const option = w.get("[data-filter=repo]").findAll("option")[1];
  expect(option.text()).toBe("airlie");
  await w.get("[data-filter=repo]").setValue("/Users/n/git/airlie");
  await flushPromises();
  expect(router.currentRoute.value.query.repo).toBe("/Users/n/git/airlie");
});
