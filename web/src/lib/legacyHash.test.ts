import { describe, expect, it } from "vitest";
import { legacyHashPath } from "./legacyHash";

describe("legacyHashPath", () => {
  it.each([
    ["#/board/AIR-17", "/board/AIR-17"],
    ["#/board", "/board"],
    ["#/join/tok123", "/join/tok123"],
    ["#/jobs/story-AIR-17", "/jobs/story-AIR-17"],
    ["#/jobs/abc", "/jobs/abc"],
    ["#/workflows/AIR-17/plan/j1", "/workflows/AIR-17/plan/j1"],
    ["#/topology/run7", "/workflows/runs/run7"],
    ["#/metrics", "/metrics"],
    ["#/signin", "/signin"],
    ["#/", "/"],
    ["#", "/"],
  ])("%s becomes %s", (hash, path) => {
    expect(legacyHashPath(hash)).toBe(path);
  });

  it.each([[""], ["#section"], ["#//evil.com"], ["#/\\evil.com"], ["#/%2f%2fevil.com"], ["#/%5cevil.com"]])(
    "%s is left alone",
    (hash) => {
      expect(legacyHashPath(hash)).toBeNull();
    },
  );

  it("never gives a path that leaves the site", () => {
    const origin = "http://app.invalid";
    for (const hash of ["#/\\evil.com", "#//evil.com", "#/%2f%2fevil.com", "#/%5Cevil.com", "#/..//evil.com", "#/a/../..//evil.com", "#/%00", "#/x\tevil"]) {
      const path = legacyHashPath(hash);
      if (path !== null) expect(new URL(path, origin).origin).toBe(origin);
    }
  });
});
