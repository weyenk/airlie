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
