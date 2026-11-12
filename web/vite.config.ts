import tailwindcss from "@tailwindcss/vite";
import vue from "@vitejs/plugin-vue";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";
import { assetsOf, buildOutDir, stampAssets, stampBuildId } from "./swBuild";

// sw.js names its cache after the build (the hash of the built index.html, which names every hashed asset) and
// precaches the assets that page loads.
function swBuildId(): Plugin {
  let outDir = "";
  return {
    name: "airlie-sw-build-id",
    apply: "build",
    configResolved(config) {
      outDir = buildOutDir(config.root, config.build.outDir);
    },
    closeBundle() {
      const html = readFileSync(outDir + "index.html");
      const id = createHash("sha256").update(html).digest("hex").slice(0, 12);
      const sw = outDir + "sw.js";
      writeFileSync(sw, stampAssets(stampBuildId(readFileSync(sw, "utf8"), id), assetsOf(new TextDecoder().decode(html))));
    },
  };
}

// The build lands inside the Python package, where `airlie serve` serves it at /.
export default defineConfig(({ mode }) => ({
  base: "/",
  plugins: [vue(), tailwindcss(), swBuildId()],
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  build: {
    outDir: "../src/airlie/serve/web_next",
    emptyOutDir: true,
  },
  server: {
    // `task web:dev` serves the app on Vite's port and sends the API to `task serve`.
    proxy: { "/v1": "http://127.0.0.1:8090" },
    // the tests alone may read the repo beside web/ (the builder's tests read the shipped config/workflows/*.yaml);
    // the dev server keeps Vite's default of web/ only
    ...(mode === "test" ? { fs: { allow: [new URL("..", import.meta.url).pathname] } } : {}),
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
}));
