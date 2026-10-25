import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import type { MetricsSummary, SeriesRow } from "@/api/metrics";
import MetricsScreen from "./MetricsScreen.vue";

const api = vi.hoisted(() => ({ metricsSummary: vi.fn(), metricsSeries: vi.fn(), listStories: vi.fn() }));
vi.mock("@/api/metrics", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/metrics")>()), metricsSummary: api.metricsSummary, metricsSeries: api.metricsSeries }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), listStories: api.listStories }));

const NOW = 1_800_000_000;
const SUMMARY: MetricsSummary = { generation_tps: 36.4, prefill_tps: 1180, stories: 14, fix_rounds_per_layer: 0.6, by: {} };
const ROWS: SeriesRow[] = [
  { t: NOW - 3 * 86400, role: "coder", p10: 20, p50: 30, p90: 40 },
  { t: NOW - 2 * 86400, role: "coder", p10: 20, p50: 29.6, p90: 40 },
];

async function open() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/metrics", component: MetricsScreen },
      { path: "/workflows", component: { template: "<i/>" } },
    ],
  });
  await router.push("/metrics");
  const w = mount(MetricsScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW * 1000 });
  setActivePinia(createPinia());
  api.metricsSummary.mockReset().mockResolvedValue(SUMMARY);
  api.metricsSeries.mockReset().mockResolvedValue(ROWS);
  api.listStories.mockReset().mockResolvedValue({ stories: [], total: 0, page: 1, size: null });
});
afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

test("it opens on 7 days sliced by role, and asks for the period before as well", async () => {
  await open();
  const week = 7 * 86400;
  expect(api.metricsSummary).toHaveBeenCalledWith({ from: NOW - week, to: NOW, slice: "role" });
  expect(api.metricsSummary).toHaveBeenCalledWith({ from: NOW - 2 * week, to: NOW - week, slice: "role" });
  expect(api.metricsSeries).toHaveBeenCalledWith({ metric: "generation_tps", from: NOW - week, to: NOW, slice: "role" });
});

test("choosing a range or a slice asks again for that window and slice", async () => {
  const { w } = await open();
  await w.get("[data-range='24h']").trigger("click");
  await flushPromises();
  expect(api.metricsSeries).toHaveBeenLastCalledWith({ metric: "generation_tps", from: NOW - 86400, to: NOW, slice: "role" });
  await w.get("[data-slice]").setValue("model");
  await flushPromises();
  expect(api.metricsSeries).toHaveBeenLastCalledWith({ metric: "generation_tps", from: NOW - 86400, to: NOW, slice: "model" });
  expect(w.text()).toContain("Generation speed by model");
});

test("All time asks from the epoch and makes no second summary call", async () => {
  const { w } = await open();
  api.metricsSummary.mockClear();
  await w.get("[data-range='all']").trigger("click");
  await flushPromises();
  expect(api.metricsSummary.mock.calls).toEqual([[{ from: 0, to: NOW, slice: "role" }]]);
});

test("the tiles show the numbers and the chart opens the Workflows list for the clicked bucket", async () => {
  const { w, router } = await open();
  expect(w.get("[data-tile=generation] [data-value]").text()).toBe("36.4tok/s");
  expect(w.get("[data-tile=stories] [data-value]").text()).toBe("14");
  await w.get("[data-plot]").trigger("keydown", { key: "ArrowRight" });
  await w.get("[data-plot]").trigger("keydown", { key: "Enter" });
  await flushPromises();
  const t = NOW - 3 * 86400;
  expect(router.currentRoute.value.fullPath).toBe(`/workflows?active_from=${t}&active_to=${t + 6 * 3600}`);
});

test("a range with no runs says so", async () => {
  api.metricsSeries.mockResolvedValue([]);
  const { w } = await open();
  expect(w.text()).toContain("No runs in this range");
});

test("a failed load shows the error and keeps the screen", async () => {
  api.metricsSummary.mockRejectedValue(new Error("missing or invalid API key"));
  const { w } = await open();
  expect(w.get("[role=alert]").text()).toContain("missing or invalid API key");
});
