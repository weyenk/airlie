import { mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import type { StoryRow } from "@/api/workflows";
import StoriesTable from "./StoriesTable.vue";

const story = (over: Partial<StoryRow>): StoryRow => ({
  key: "AIR-17",
  title: "Board card modal",
  repo: "weyenk/airlie",
  stage: "build",
  blocked: null,
  last_activity: Date.now() / 1000 - 120,
  ...over,
});

async function show(stories: StoryRow[], empty = "No stories yet") {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });
  await router.push("/workflows");
  const w = mount(StoriesTable, { props: { stories, empty }, global: { plugins: [router] } });
  return { w, router };
}

test("a row shows key, title, stage, repository and last activity, and opens the story", async () => {
  const { w, router } = await show([story({})]);
  const row = w.get("[data-story=AIR-17]");
  expect(row.text()).toContain("Board card modal");
  expect(row.text()).toContain("build");
  expect(row.text()).toContain("weyenk/airlie");
  expect(row.text()).toContain("2 min ago");
  await row.trigger("click");
  await new Promise((r) => setTimeout(r));
  expect(router.currentRoute.value.fullPath).toBe("/workflows/AIR-17");
});

test("a blocked row shows its reason and a blocked chip; an unblocked one shows neither", async () => {
  const { w } = await show([story({ key: "AIR-30", stage: "layers", blocked: "layer 02 failed its gate twice" }), story({})]);
  const blocked = w.get("[data-story=AIR-30]");
  expect(blocked.text()).toContain("Blocked: layer 02 failed its gate twice");
  expect(blocked.text()).toContain("blocked");
  expect(w.get("[data-story=AIR-17]").text()).not.toContain("Blocked");
});

test("long keys, titles and repositories truncate with their full text on hover", async () => {
  const long = "w".repeat(200);
  const { w } = await show([story({ key: "HDS-1234", title: long, repo: "o".repeat(100) + "/name" })]);
  expect(w.get(`[title='${long}']`).classes()).toContain("truncate");
  expect(w.get(`[title='${"o".repeat(100)}/name']`).classes()).toContain("truncate");
  expect(w.get("[data-story]").text()).toContain("HDS-1234");
});

test("no stories says why", async () => {
  const { w } = await show([], "No stories match");
  expect(w.text()).toContain("No stories match");
});
