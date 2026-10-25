import { mount } from "@vue/test-utils";
import type { MetricsSummary } from "@/api/metrics";
import type { StoryRow } from "@/api/workflows";
import { RANGES } from "./math";
import StatTiles from "./StatTiles.vue";

const sum = (over: Partial<MetricsSummary> = {}): MetricsSummary => ({
  generation_tps: 32,
  prefill_tps: 1236,
  stories: 14,
  fix_rounds_per_layer: 0.6,
  by: {},
  ...over,
});
const story = (key: string, stage: string | null, blocked: string | null = null): StoryRow => ({ key, title: null, repo: null, stage, blocked, last_activity: 1 });
const STORIES = [story("A-1", "review"), story("A-2", "publish"), story("A-3", "layers"), story("A-4", "layers", "stuck")];

function show(props: { summary?: MetricsSummary; previous?: MetricsSummary | null; range?: (typeof RANGES)[number]; stories?: StoryRow[] }) {
  return mount(StatTiles, { props: { summary: sum(), previous: sum({ generation_tps: 40, prefill_tps: 1200, fix_rounds_per_layer: 0.6 }), range: RANGES[2], stories: STORIES, ...props } });
}
const tile = (w: ReturnType<typeof show>, name: string) => w.get(`[data-tile=${name}]`);

test("the four tiles show their values and units", () => {
  const w = show({});
  expect(tile(w, "generation").get("[data-value]").text()).toBe("32tok/s");
  expect(tile(w, "prefill").get("[data-value]").text()).toBe("1,236tok/s");
  expect(tile(w, "stories").get("[data-value]").text()).toBe("14");
  expect(tile(w, "fix-rounds").get("[data-value]").text()).toBe("0.6");
});

test("generation and prefill compare with the period before: a drop is down, a rise is up", () => {
  const w = show({});
  expect(tile(w, "generation").get("[data-delta]").text()).toBe("▼ 20% vs the 7 days before");
  expect(tile(w, "prefill").get("[data-delta]").text()).toBe("▲ 3% vs the 7 days before");
  expect(tile(w, "fix-rounds").get("[data-delta]").text()).toBe("same as the 7 days before");
});

test("a previous window with nothing in it, or a zero, shows no percentage and no Infinity or NaN", () => {
  const w = show({ previous: sum({ generation_tps: null, prefill_tps: 0, fix_rounds_per_layer: null }) });
  expect(tile(w, "generation").get("[data-delta]").text()).toBe("no data for the 7 days before");
  expect(tile(w, "prefill").get("[data-delta]").text()).toBe("▲ from 0 in the 7 days before");
  expect(w.text()).not.toMatch(/Infinity|NaN/);
});

test("All time has no period before", () => {
  const w = show({ previous: null, range: RANGES[4] });
  expect(tile(w, "generation").get("[data-delta]").text()).toBe("all time");
});

test("a missing value reads unknown, never 0", () => {
  const w = show({ summary: sum({ generation_tps: null, prefill_tps: null, fix_rounds_per_layer: null }) });
  expect(tile(w, "generation").get("[data-value]").text()).toBe("unknown");
  expect(tile(w, "prefill").get("[data-value]").text()).toBe("unknown");
  expect(tile(w, "fix-rounds").get("[data-value]").text()).toBe("unknown");
});

test("the stories tile says how many are in each state, blocked ones apart", () => {
  const w = show({});
  expect(tile(w, "stories").get("[data-delta]").text()).toBe("2 in review · 1 building · 1 blocked");
});
