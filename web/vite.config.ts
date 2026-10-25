import tailwindcss from "@tailwindcss/vite";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

// The build lands inside the Python package, where `airlie serve` serves it at /next.
export default defineConfig({
  base: "/next/",
  plugins: [vue(), tailwindcss()],
  resolve: { alias: { "@": new URL("./src", import.meta.url).pathname } },
  build: {
    outDir: "../src/airlie/serve/web_next",
    emptyOutDir: true,
  },
  server: {
    // `task web:dev` serves the app on Vite's port and sends the API to `task serve`.
    proxy: { "/v1": "http://127.0.0.1:8090" },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
