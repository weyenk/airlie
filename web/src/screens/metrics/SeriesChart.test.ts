import { mount } from "@vue/test-utils";
import type { SeriesRow } from "@/api/metrics";
import SeriesChart from "./SeriesChart.vue";

const DAY = 86400;
const ROWS: SeriesRow[] = [
  { t: 0, role: "coder", p10: 20, p50: 30, p90: 40 },
  { t: 0, role: "tester", p10: 10, p50: 24.4, p90: 30 },
  { t: DAY, role: "coder", p10: 20, p50: 29.6, p90: 40 },
  { t: 2 * DAY, role: "coder", p10: 20, p50: 35, p90: 40 },
  { t: 2 * DAY, role: "unknown", p10: 1, p50: 5, p90: 9 },
];
const props = { rows: ROWS, slice: "role", window: { from: 0, to: 3 * DAY }, bucket: DAY, title: "Generation speed by role" };

test("one line per slice, in the series colours, each named in the legend", () => {
  const w = mount(SeriesChart, { props });
  const lines = w.findAll("[data-line]");
  expect(lines.map((l) => l.attributes("data-line"))).toEqual(["coder", "tester", "unknown"]);
  expect(lines.map((l) => l.attributes("stroke"))).toEqual(["#34d399", "#38bdf8", "#fb923c"]);
  expect(w.get("[data-legend]").text()).toContain("coder");
  expect(w.get("[data-legend]").text()).toContain("unknown");
});

test("an empty series says there were no runs and draws no chart", () => {
  const w = mount(SeriesChart, { props: { ...props, rows: [] } });
  expect(w.text()).toContain("No runs in this range");
  expect(w.find("svg").exists()).toBe(false);
});

test("the point under the pointer gets a tooltip with every slice's value, unknown where a slice has none", async () => {
  const w = mount(SeriesChart, { props });
  const plot = w.get("[data-plot]");
  expect(w.find("[data-tip]").exists()).toBe(false);
  // 800px wide in jsdom: the day at t=DAY sits a third of the way along the plot
  await plot.trigger("mousemove", { clientX: 40 + (800 - 48 - 40) / 3 });
  const tip = w.get("[data-tip]");
  expect(tip.text()).toContain("coder");
  expect(tip.text()).toContain("29.6");
  expect(tip.text()).toContain("tester");
  expect(tip.text()).toContain("unknown");
  expect(tip.text()).not.toMatch(/NaN|undefined/);
  await plot.trigger("mouseleave");
  expect(w.find("[data-tip]").exists()).toBe(false);
});

test("a click on a bucket asks to open the stories active in that bucket", async () => {
  const w = mount(SeriesChart, { props });
  const plot = w.get("[data-plot]");
  await plot.trigger("mousemove", { clientX: 40 + ((800 - 48 - 40) * 2) / 3 });
  await plot.trigger("click", { clientX: 40 + ((800 - 48 - 40) * 2) / 3 });
  expect(w.emitted("open")).toEqual([[{ active_from: String(2 * DAY), active_to: String(3 * DAY) }]]);
});

test("the keyboard reaches the buckets too: arrows move, Enter opens", async () => {
  const w = mount(SeriesChart, { props });
  const plot = w.get("[data-plot]");
  expect(plot.attributes("tabindex")).toBe("0");
  await plot.trigger("keydown", { key: "ArrowRight" });
  expect(w.get("[data-tip]").text()).toContain("coder");
  await plot.trigger("keydown", { key: "ArrowRight" });
  await plot.trigger("keydown", { key: "Enter" });
  expect(w.emitted("open")).toEqual([[{ active_from: String(DAY), active_to: String(2 * DAY) }]]);
});

test("a value that is not a number is skipped, not drawn as 0 or NaN", () => {
  const rows = [...ROWS, { t: 2 * DAY, role: "tester", p10: 0, p50: Number.NaN, p90: 0 }];
  const w = mount(SeriesChart, { props: { ...props, rows } });
  expect(w.html()).not.toMatch(/NaN/);
});

test("colours follow the slice's name, not the order the rows came in", () => {
  const w = mount(SeriesChart, { props: { ...props, rows: [...ROWS].reverse() } });
  expect(w.get("[data-line=coder]").attributes("stroke")).toBe("#34d399");
  expect(w.get("[data-line=tester]").attributes("stroke")).toBe("#38bdf8");
});

test("past four slices the colours repeat with a dash, so no two lines look alike", () => {
  const names = ["coder", "tester", "reviewer", "chief", "zeta"];
  const rows = names.map((n, i) => ({ t: 0, role: n, p10: 1, p50: i + 1, p90: 9 }));
  const w = mount(SeriesChart, { props: { ...props, rows } });
  const look = (n: string) => `${w.get(`[data-line=${n}]`).attributes("stroke")}|${w.get(`[data-line=${n}]`).attributes("stroke-dasharray") ?? ""}`;
  expect(new Set(names.map(look)).size).toBe(5);
  expect(w.get("[data-line=coder]").attributes("stroke-dasharray")).toBeUndefined();
});

test("a missing bucket breaks the line instead of joining across the gap", () => {
  const rows: SeriesRow[] = [
    { t: 0, role: "coder", p10: 1, p50: 10, p90: 20 },
    { t: DAY, role: "coder", p10: 1, p50: 12, p90: 20 },
    { t: 5 * DAY, role: "coder", p10: 1, p50: 14, p90: 20 },
  ];
  const w = mount(SeriesChart, { props: { ...props, rows, window: { from: 0, to: 6 * DAY } } });
  expect(w.get("[data-line=coder]").attributes("d")?.match(/M/g)).toHaveLength(2);
});

test("when a refresh moves the buckets the pointer stays on the nearest one that is left", async () => {
  const w = mount(SeriesChart, { props });
  await w.get("[data-plot]").trigger("keydown", { key: "ArrowRight" });
  await w.get("[data-plot]").trigger("keydown", { key: "ArrowRight" }); // t = DAY
  const moved = ROWS.map((r) => ({ ...r, t: r.t + 600 }));
  await w.setProps({ rows: moved });
  expect(w.get("[data-tip]").text()).toContain("29.6");
  await w.get("[data-plot]").trigger("keydown", { key: "Enter" });
  expect(w.emitted("open")?.at(-1)).toEqual([{ active_from: String(DAY + 600), active_to: String(2 * DAY + 600) }]);
  await w.setProps({ rows: [] });
  expect(w.find("[data-tip]").exists()).toBe(false);
});
