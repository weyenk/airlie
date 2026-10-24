// The metrics tab's two routes (`/v1/metrics`).

import { queryString, request } from "./index";

export type MetricSlice = "role" | "model" | "topology" | "task" | "agent";
export type MetricName = "generation_tps" | "prefill_tps";

export type MetricsSummary = {
  generation_tps: number | null;
  prefill_tps: number | null;
  stories: number;
  fix_rounds_per_layer: number | null;
  /** One entry per slice value present in the range. */
  by: Record<string, { generation_tps: number | null; prefill_tps: number | null }>;
};

/** One row per bucket: `t` (epoch seconds), the slice's name holding its value, and the quantiles. */
export type SeriesRow = { t: number; p10: number; p50: number; p90: number } & { [slice: string]: string | number };

type Range = { from?: number; to: number; slice?: MetricSlice };

export function metricsSummary({ from, to, slice }: Range): Promise<MetricsSummary> {
  return request<MetricsSummary>(`/v1/metrics/summary${queryString({ from, to, slice })}`);
}

export function metricsSeries({ metric, from, to, slice }: Range & { metric?: MetricName }): Promise<SeriesRow[]> {
  return request<SeriesRow[]>(`/v1/metrics/series${queryString({ metric, from, to, slice })}`);
}
