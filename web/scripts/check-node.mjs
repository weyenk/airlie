// Stops early, saying which node it found, when the first node on the PATH is not the major web/package.json pins
// in engines.node (npm itself only warns about that).
import { readFileSync } from "node:fs";

const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
const wanted = Number.parseInt(/\d+/.exec(pkg.engines.node)?.[0] ?? "", 10);
const found = process.versions.node;

if (Number.parseInt(found, 10) !== wanted) {
  console.error(`node ${found} (${process.execPath}) is the first node on the PATH; airlie's web app needs node ${wanted} first on the PATH`);
  console.error(`install node ${wanted} and put its bin directory ahead of the other node's in your PATH`);
  process.exit(1);
}
