// The build-id stamp vite.config.ts puts in the built sw.js, kept apart so the tests can reach it.
import { fileURLToPath, pathToFileURL } from "node:url";

export const BUILD_TOKEN = "__AIRLIE_BUILD__";

/** The build's output folder as a path with a trailing slash (a file URL's `pathname` would escape spaces). */
export function buildOutDir(root: string, outDir: string): string {
  return fileURLToPath(new URL(outDir + "/", pathToFileURL(root + "/")));
}

/** sw.js with its token swapped for the build id; a worker without the token would keep one cache name forever. */
export function stampBuildId(src: string, id: string): string {
  if (!src.includes(BUILD_TOKEN)) {
    throw new Error(`sw.js has no ${BUILD_TOKEN} to stamp: its cache would never change between builds`);
  }
  return src.replace(BUILD_TOKEN, id);
}

export const ASSETS_TOKEN = "__AIRLIE_ASSETS__";

/** The hashed files the built index.html loads (entry chunk, preloads, stylesheet), once each, in page order. */
export function assetsOf(html: string): string[] {
  const found = new Set<string>();
  for (const m of html.matchAll(/\b(?:src|href)="(\/assets\/[^"]+)"/g)) found.add(m[1]);
  return [...found];
}

/** sw.js with its empty asset list swapped for the built one; without the placeholder the first offline launch would be blank. */
export function stampAssets(src: string, assets: string[]): string {
  const placeholder = `/*${ASSETS_TOKEN}*/ []`;
  if (!src.includes(placeholder)) {
    throw new Error(`sw.js has no ${ASSETS_TOKEN} list to stamp: the built chunks would not be precached`);
  }
  return src.replace(placeholder, JSON.stringify(assets));
}
