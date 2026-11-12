import { assetsOf, buildOutDir, stampAssets, stampBuildId } from "../swBuild";

test("the token is replaced by the build id", () => {
  expect(stampBuildId('const VERSION = "__AIRLIE_BUILD__";', "abc123")).toBe('const VERSION = "abc123";');
});

test("a worker without the token fails the build instead of shipping a fixed cache name", () => {
  expect(() => stampBuildId('const VERSION = "abc123";', "def456")).toThrow(/__AIRLIE_BUILD__/);
});

test("the output folder keeps spaces as they are", () => {
  expect(buildOutDir("/Users/a b/web", "../src/web_next")).toBe("/Users/a b/src/web_next/");
});

const BUILT = `<!doctype html><html><head>
<script type="module" crossorigin src="/assets/index-AbC123.js"></script>
<link rel="modulepreload" crossorigin href="/assets/vendor-9xY.js">
<link rel="stylesheet" crossorigin href="/assets/index-Q1w2.css">
<link rel="icon" href="/favicon.ico">
<link rel="manifest" href="/manifest.webmanifest">
<script type="module" src="/assets/index-AbC123.js"></script>
</head></html>`;

test("the built page's entry chunk, preloads and stylesheet are the assets to precache, once each", () => {
  expect(assetsOf(BUILT)).toEqual(["/assets/index-AbC123.js", "/assets/vendor-9xY.js", "/assets/index-Q1w2.css"]);
});

test("the asset list replaces its placeholder with JSON", () => {
  const src = "const ASSETS = /*__AIRLIE_ASSETS__*/ [];";
  expect(stampAssets(src, ["/assets/a.js"])).toBe('const ASSETS = ["/assets/a.js"];');
});

test("a worker without the asset placeholder fails the build instead of skipping the precache", () => {
  expect(() => stampAssets("const ASSETS = [];", [])).toThrow(/__AIRLIE_ASSETS__/);
});
