// vite.config.ts runs in Node, but the app takes no @types/node: name just the three calls its sw.js stamp makes.
declare const process: { env: Record<string, string | undefined> };
declare module "node:crypto" {
  export function createHash(algorithm: string): { update(data: Uint8Array): { digest(encoding: "hex"): string } };
}
declare module "node:url" {
  export function fileURLToPath(url: URL): string;
  export function pathToFileURL(path: string): URL;
}
declare module "node:fs" {
  export function readFileSync(path: string): Uint8Array;
  export function readFileSync(path: string, encoding: "utf8"): string;
  export function writeFileSync(path: string, data: string): void;
}
