import { activityWindow, bucketSeconds, compare, formatRate, niceTicks, previousWindow, RANGES, windowOf } from "./math";

test("the bucket size follows the server's: 4 h to 5 min, 24 h to 1 h, 7 d to 6 h, longer to a day", () => {
  expect(bucketSeconds(4 * 3600)).toBe(300);
  expect(bucketSeconds(24 * 3600)).toBe(3600);
  expect(bucketSeconds(7 * 86400)).toBe(6 * 3600);
  expect(bucketSeconds(30 * 86400)).toBe(86400);
  expect(bucketSeconds(1_700_000_000)).toBe(86400);
});

test("a range ends now; All time starts at the epoch and has no earlier period", () => {
  expect(windowOf(RANGES[2], 1000_000)).toEqual({ from: 1000_000 - 7 * 86400, to: 1000_000 });
  expect(windowOf(RANGES[4], 1000_000)).toEqual({ from: 0, to: 1000_000 });
  expect(previousWindow(RANGES[2], { from: 400_000, to: 1_004_800 })).toEqual({ from: 400_000 - 604_800, to: 400_000 });
  expect(previousWindow(RANGES[4], { from: 0, to: 5 })).toBeNull();
});

test("the change against the previous window", () => {
  expect(compare(32, 40)).toEqual({ kind: "down", percent: 20 });
  expect(compare(1236, 1200)).toEqual({ kind: "up", percent: 3 });
  expect(compare(36.4, 36.3)).toEqual({ kind: "flat", percent: 0 });
});

test("an unknown or zero previous value never makes Infinity or NaN", () => {
  expect(compare(5, null)).toEqual({ kind: "none", percent: null });
  expect(compare(null, 5)).toEqual({ kind: "none", percent: null });
  expect(compare(Number.NaN, 5)).toEqual({ kind: "none", percent: null });
  expect(compare(5, 0)).toEqual({ kind: "up", percent: null });
  expect(compare(0, 0)).toEqual({ kind: "flat", percent: 0 });
});

test("a click on a bucket opens the activity window from the bucket's start to its end", () => {
  expect(activityWindow(1_000_000, 21_600)).toEqual({ active_from: "1000000", active_to: "1021600" });
});

test("rates read with a thousands separator and one decimal, and unknown when missing", () => {
  expect(formatRate(1180.04)).toBe("1,180");
  expect(formatRate(36.44)).toBe("36.4");
  expect(formatRate(null)).toBe("unknown");
  expect(formatRate(Number.NaN)).toBe("unknown");
});

test("axis ticks start at zero and end at or above the top", () => {
  const t = niceTicks(41.1);
  expect(t[0]).toBe(0);
  expect(t.at(-1)).toBeGreaterThanOrEqual(41.1);
  expect(niceTicks(0).length).toBeGreaterThan(1);
});
