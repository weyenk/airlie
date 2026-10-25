// The Metrics screen's arithmetic: the windows a range stands for, the change against the period before, and
// the chart's axes. No Vue in here.

export type Range = { value: string; label: string; before: string; seconds: number | null };
export const RANGES: readonly Range[] = [
  { value: "4h", label: "4 hours", before: "4 hours before", seconds: 4 * 3600 },
  { value: "24h", label: "24 hours", before: "24 hours before", seconds: 24 * 3600 },
  { value: "7d", label: "7 days", before: "7 days before", seconds: 7 * 86400 },
  { value: "30d", label: "30 days", before: "30 days before", seconds: 30 * 86400 },
  { value: "all", label: "All time", before: "", seconds: null },
];
export const DEFAULT_RANGE = RANGES[2];

export type Window = { from: number; to: number };

/** The bucket width the server gives a window of `span` seconds (src/airlie/serve/app.py `_metric_bucket_s`). */
export function bucketSeconds(span: number): number {
  if (span <= 4 * 3600) return 300;
  if (span <= 24 * 3600) return 3600;
  if (span <= 7 * 86400) return 6 * 3600;
  return 86400;
}

/** The window `range` stands for when it is `now` (epoch seconds). All time starts at the epoch. */
export function windowOf(range: Range, now: number): Window {
  return { from: range.seconds === null ? 0 : now - range.seconds, to: now };
}

/** The window of the same length just before `window`; All time has none. */
export function previousWindow(range: Range, window: Window): Window | null {
  if (range.seconds === null) return null;
  const span = window.to - window.from;
  return { from: window.from - span, to: window.from };
}

export type Change = { kind: "up" | "down" | "flat" | "none"; percent: number | null };

const known = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);

/** `current` against `previous`. `none`: one is unknown. From zero there is no percentage (`percent` null). */
export function compare(current: number | null | undefined, previous: number | null | undefined): Change {
  if (!known(current) || !known(previous)) return { kind: "none", percent: null };
  if (previous === 0) return current === 0 ? { kind: "flat", percent: 0 } : { kind: current > 0 ? "up" : "down", percent: null };
  const percent = Math.round(Math.abs(((current - previous) / previous) * 100));
  if (percent === 0) return { kind: "flat", percent: 0 };
  return { kind: current > previous ? "up" : "down", percent };
}

/** The Workflows list's query for the stories active in the bucket that starts at `t`. */
export function activityWindow(t: number, bucket: number): { active_from: string; active_to: string } {
  return { active_from: String(Math.round(t)), active_to: String(Math.round(t + bucket)) };
}

/** A rate or count for a tile or tooltip: "1,180", "36.4", or "unknown". */
export function formatRate(v: number | null | undefined): string {
  if (!known(v)) return "unknown";
  return v >= 100 ? Math.round(v).toLocaleString("en-US") : v.toLocaleString("en-US", { maximumFractionDigits: 1 });
}

/** Axis ticks from 0 to a round number at or above `max`. */
export function niceTicks(max: number): number[] {
  const top = known(max) && max > 0 ? max : 1;
  const raw = top / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = 0; ; v += step) {
    out.push(Math.round(v * 1e6) / 1e6);
    if (v >= top) break;
  }
  return out;
}
