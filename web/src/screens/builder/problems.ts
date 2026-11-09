// Which box a validation problem is about. `check_workflows` (airlie.flow.spec) writes "workflow <w>: node <n>:
// <what>", and "workflow <w>: layout names unknown node '<n>'" for a place left for a box that is gone. Pure.

const NODE = /^workflow [^:]+: node ([^\s:]+): /;
const LAYOUT = /^workflow [^:]+: layout names unknown node '([^']+)'/;

/** The node a problem names, or null when it is about the workflow as a whole. */
export function problemNode(problem: string): string | null {
  return NODE.exec(problem)?.[1] ?? LAYOUT.exec(problem)?.[1] ?? null;
}

const EDGE = /^workflow [^:]+: node ([^\s:]+): (?:the )?(?:loop )?line to '?([^\s':]+)'?[: ]/;

/** The line a problem is about, or null: the composer's checks name a line "line to <target>" (a condition that
 * does not read, a loop that goes the wrong way, a cycle with no bound) under its source's node. */
export function problemEdge(problem: string): { from: string; to: string } | null {
  const m = EDGE.exec(problem);
  return m ? { from: m[1], to: m[2] } : null;
}

/** The key a line is known by in `problemsByEdge`. */
export const edgeKey = (from: string, to: string): string => `${from}>${to}`;

/** The workflow a problem is about (`workflow <w>: ...`), or null when it names none. */
export function problemWorkflow(problem: string): string | null {
  return /^workflow ([^:]+): /.exec(problem)?.[1] ?? null;
}

/** Whether a problem belongs to `workflow`: the server checks a bundle's children with the parent, and a child's
 * node or line of the same name is not the parent's. A problem that names no workflow is kept. */
function about(problem: string, workflow: string | undefined): boolean {
  const named = problemWorkflow(problem);
  return workflow === undefined || named === null || named === workflow;
}

/** The problems that name a line, by `edgeKey`, each as its `<what>`; with `workflow`, only that workflow's. */
export function problemsByEdge(problems: string[], workflow?: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const p of problems) {
    if (!about(p, workflow)) continue;
    const e = problemEdge(p);
    if (e) (out[edgeKey(e.from, e.to)] ??= []).push(p.replace(NODE, ""));
  }
  return out;
}

/** The problems that name a node, by node, each as its `<what>` (the box shows what is wrong with it); with
 * `workflow`, only that workflow's. */
export function problemsByNode(problems: string[], workflow?: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const p of problems) {
    if (!about(p, workflow)) continue;
    const id = problemNode(p);
    if (id === null) continue;
    const what = p.replace(NODE, "");
    (out[id] ??= []).push(what);
  }
  return out;
}
