// Which box a validation problem is about. `check_workflows` (airlie.flow.spec) writes "workflow <w>: node <n>:
// <what>", and "workflow <w>: layout names unknown node '<n>'" for a place left for a box that is gone. Pure.

const NODE = /^workflow [^:]+: node ([^\s:]+): /;
const LAYOUT = /^workflow [^:]+: layout names unknown node '([^']+)'/;

/** The node a problem names, or null when it is about the workflow as a whole. */
export function problemNode(problem: string): string | null {
  return NODE.exec(problem)?.[1] ?? LAYOUT.exec(problem)?.[1] ?? null;
}

/** The problems that name a node, by node, each as its `<what>` (the box shows what is wrong with it). */
export function problemsByNode(problems: string[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const p of problems) {
    const id = problemNode(p);
    if (id === null) continue;
    const what = p.replace(NODE, "");
    (out[id] ??= []).push(what);
  }
  return out;
}
