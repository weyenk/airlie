import { safeNext } from "./next";

test("an app path is followed, query and all", () => {
  expect(safeNext("/board")).toBe("/board");
  expect(safeNext("/workflows/runs/r1?tab=x#top")).toBe("/workflows/runs/r1?tab=x#top");
  expect(safeNext("/")).toBe("/");
});

test.each([
  ["//evil.com"],
  ["///evil.com"],
  ["/\\evil.com"],
  ["/\t/evil.com"],
  ["/\n/evil.com"],
  ["/next/..//evil.com"],
  ["/%5Cevil.com"],
  ["/%5C"],
  ["https://evil.com/x"],
  ["http://evil.com"],
  ["javascript:alert(1)"],
  ["evil.com"],
  [""],
  [undefined],
  [null],
  [["/board", "/x"]],
])("%j is never followed: sign-in lands on the chat page", (raw) => {
  expect(safeNext(raw)).toBe("/");
});

test("sign-in itself is not a place to land", () => {
  expect(safeNext("/signin?next=/board")).toBe("/");
});
