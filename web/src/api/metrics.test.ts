import { callOf, stubJson } from "./fetch-stub";
import { metricsSeries, metricsSummary } from "./metrics";

afterEach(() => vi.unstubAllGlobals());

test("the summary asks for a range sliced by something", async () => {
  const fetchMock = stubJson({ generation_tps: 1, by: {} });

  await metricsSummary({ from: 100, to: 200, slice: "model" });
  await metricsSummary({ to: 200 });

  expect(callOf(fetchMock, 0).url).toBe("/v1/metrics/summary?from=100&to=200&slice=model");
  expect(callOf(fetchMock, 1).url).toBe("/v1/metrics/summary?to=200");
});

test("the series asks for a metric over a range", async () => {
  const fetchMock = stubJson([{ t: 1, role: "coder", p10: 1, p50: 2, p90: 3 }]);

  await expect(metricsSeries({ metric: "prefill_tps", from: 100, to: 200, slice: "role" })).resolves.toHaveLength(1);

  expect(callOf(fetchMock).url).toBe("/v1/metrics/series?metric=prefill_tps&from=100&to=200&slice=role");
});
