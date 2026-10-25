import { ago, initials, passkeys, shortDate } from "./people";

test("initials: first and last word, or the first two letters", () => {
  expect(initials("Noah Kruswicki")).toBe("NK");
  expect(initials("Jess")).toBe("JE");
  expect(initials("  ")).toBe("?");
});

test("ago counts minutes, then hours, then gives the date", () => {
  const now = 1_000_000;
  expect(ago(now - 10, now)).toBe("1 min ago");
  expect(ago(now - 600, now)).toBe("10 min ago");
  expect(ago(now - 2 * 3600, now)).toBe("2 h ago");
  expect(ago(now - 3 * 86400, now)).toBe(shortDate(now - 3 * 86400));
});

test("passkeys is singular for one", () => {
  expect([0, 1, 2].map(passkeys)).toEqual(["0 passkeys", "1 passkey", "2 passkeys"]);
});
