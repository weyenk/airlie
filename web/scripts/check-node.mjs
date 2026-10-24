// Stops early, with one clear line, when the node on the path is not the major web/package.json pins in
// engines.node (npm itself only warns about that).
import { readFileSync } from "node:fs";

const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
const wanted = Number.parseInt(/\d+/.exec(pkg.engines.node)?.[0] ?? "", 10);
const found = process.versions.node;

if (Number.parseInt(found, 10) !== wanted) {
  console.error(`node ${found} is on the path; airlie's web app needs node ${wanted}`);
  process.exit(1);
}
