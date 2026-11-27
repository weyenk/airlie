import { OIDC_START, OIDC_UNBOUND_FLAG, ssoStartHref, ssoTarget } from "./oidc";

// A second implementation of the same rule lives on the server (`_STUDIO_ROUTES` / `safe_target`,
// auth/oidc_flow.py) and it is the one that decides where a browser ends up. What is checked here is the thing
// only this side can check: the link the person clicks never names a destination the server is about to refuse,
// and a target that could leave the site names nothing at all.

test("an app route is carried as the target to resume", () => {
  expect(ssoTarget("/board/AIR-17")).toBe("/board/AIR-17");
  expect(ssoStartHref("/board/AIR-17")).toBe(`${OIDC_START}?target=%2Fboard%2FAIR-17`);
});

test("a route's own query and fragment ride along", () => {
  expect(ssoTarget("/workflows/runs/r1/node?tab=log")).toBe("/workflows/runs/r1/node?tab=log");
  expect(ssoTarget("/metrics#cpu")).toBe("/metrics#cpu");
});

test.each([
  ["//evil.com"],
  ["/\\evil.com"],
  ["https://evil.com/x"],
  ["javascript:alert(1)"],
  ["/%2f%2fevil.com"],
  ["/board/../../admin"],
  ["/boardadmin"],
  ["/metrics-something"],
  ["/nowhere/else"],
  ["/signin"],
  ["board/AIR-17"],
  ["/ pad"],
  ["/board\nHost: evil"],
  ["/board\u0000"],
  ["/".concat("a".repeat(2048))],
  [undefined],
  [null],
  [42],
])("target %p is not a destination the link offers", (raw) => {
  expect(ssoTarget(raw)).toBe("");
  expect(ssoStartHref(raw)).toBe(OIDC_START);
});

test("the root needs no target parameter", () => {
  expect(ssoTarget("/")).toBe("/");
  expect(ssoStartHref("/")).toBe(OIDC_START);
  expect(ssoStartHref(undefined)).toBe(OIDC_START);
});

test("the start path is the server's route", () => {
  expect(OIDC_START).toBe("/v1/auth/oidc/start");
});

test("a carried flag rides along with the target", () => {
  expect(ssoStartHref("/board", "1")).toBe("/v1/auth/oidc/start?target=%2Fboard&oidc_unbound=1");
  expect(ssoStartHref(undefined, "1")).toBe(`${OIDC_START}?oidc_unbound=1`);
});

test("only the flag itself may be carried", () => {
  // undefined means "nothing to carry"; anything else is this page's own flag value, repeated. Arbitrary query
  // values must not be forwarded, because /start's query is sent on to the IdP's authorize endpoint.
  expect(ssoStartHref("/board", undefined)).toBe(`${OIDC_START}?target=%2Fboard`);
  expect(ssoStartHref("/board", "")).toBe(`${OIDC_START}?target=%2Fboard&oidc_unbound=`);
});

test("the flag is named for the screen that reads it", () => {
  expect(OIDC_UNBOUND_FLAG).toBe("oidc_unbound");
});
