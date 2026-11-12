import { buildOutDir, stampBuildId } from "../swBuild";

test("the token is replaced by the build id", () => {
  expect(stampBuildId('const VERSION = "__AIRLIE_BUILD__";', "abc123")).toBe('const VERSION = "abc123";');
});

test("a worker without the token fails the build instead of shipping a fixed cache name", () => {
  expect(() => stampBuildId('const VERSION = "abc123";', "def456")).toThrow(/__AIRLIE_BUILD__/);
});

test("the output folder keeps spaces as they are", () => {
  expect(buildOutDir("/Users/a b/web", "../src/web_next")).toBe("/Users/a b/src/web_next/");
});
