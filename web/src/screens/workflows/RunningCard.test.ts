import { mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import type { RunningRow } from "@/api/workflows";
import RunningCard from "./RunningCard.vue";

const row = (over: Partial<RunningRow>): RunningRow => ({
  kind: "story",
  key: "AIR-17",
  title: "Board card modal",
  topology: null,
  progress: 0.62,
  last_activity: 1,
  ...over,
});

function show(rows: RunningRow[]) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });
  return mount(RunningCard, { props: { rows }, global: { plugins: [router] } });
}

test("a story row links to its workflow view with its key, title and progress", () => {
  const w = show([row({})]);
  const link = w.get("a");
  expect(link.attributes("href")).toBe("/workflows/AIR-17");
  expect(w.text()).toContain("AIR-17");
  expect(w.text()).toContain("Board card modal");
  expect(w.get("[role=progressbar]").attributes("aria-valuenow")).toBe("62");
});

test("a run row links to the run view, tagged with its topology, and an unknown progress slides", () => {
  const w = show([row({ kind: "run", key: "20261024-abc", title: "Fix the flaky test", topology: "studio", progress: null })]);
  expect(w.get("a").attributes("href")).toBe("/workflows/runs/20261024-abc");
  expect(w.text()).toContain("studio run");
  expect(w.text()).not.toContain("20261024-abc");
  expect(w.find("[data-indeterminate]").exists()).toBe(true);
});

test("nothing running says so, and the count shows", () => {
  expect(show([]).text()).toContain("Nothing is running");
  expect(show([row({}), row({ key: "AIR-2" })]).get("[data-count]").text()).toBe("2");
});

test("a long title truncates and keeps its full text on hover", () => {
  const long = "w".repeat(200);
  const w = show([row({ title: long, key: "HDS-1234" })]);
  const title = w.get(`[title='${long}']`);
  expect(title.classes()).toContain("truncate");
});
