import { ago } from "./ago";

const NOW = new Date(2026, 10, 12, 15, 0, 0); // Thursday 12 Nov 2026, local
const secs = (d: Date) => d.getTime() / 1000;
const before = (ms: number) => secs(new Date(NOW.getTime() - ms));
const MIN = 60_000;
const HOUR = 60 * MIN;

test("under a minute reads now", () => {
  expect(ago(before(30_000), NOW.getTime())).toBe("now");
});

test("minutes and hours of today read as 5m and 2h", () => {
  expect(ago(before(5 * MIN), NOW.getTime())).toBe("5m");
  expect(ago(before(2 * HOUR), NOW.getTime())).toBe("2h");
});

test("the day before reads yesterday, even a few hours ago across midnight", () => {
  expect(ago(secs(new Date(2026, 10, 11, 23, 0)), NOW.getTime())).toBe("yesterday");
  expect(ago(secs(new Date(2026, 10, 11, 2, 0)), NOW.getTime())).toBe("yesterday");
});

test("within the week reads the weekday, older reads the date", () => {
  expect(ago(secs(new Date(2026, 10, 9, 12, 0)), NOW.getTime())).toBe("Mon");
  expect(ago(secs(new Date(2026, 9, 20, 12, 0)), NOW.getTime())).toBe("Oct 20");
});
