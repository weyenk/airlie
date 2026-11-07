// A workflow as the builder draws it: boxes (a node each) and wires (the moves between them). Pure: no Vue, so the
// model tests fast. The YAML model is airlie.flow.spec (WorkflowSpec, NodeSpec); the wires are derived the way the
// composer walks it (airlie.flow.composer `_route`).

export const NODE_TYPES = ["ask", "conversation", "write", "meeting", "check", "gate", "parallel", "each"] as const;
export type NodeType = (typeof NODE_TYPES)[number];

/** A node's fields as the YAML has them, `type` aside (the box carries it). */
export type NodeSpec = Record<string, unknown>;
export type Layout = Record<string, { x: number; y: number }>;

/** One workflow's mapping in the YAML, its layout aside. */
export type WorkflowSpec = {
  type?: string | null;
  start: string;
  loose?: string[];
  nodes: Record<string, NodeSpec & { type: NodeType }>;
};

/** A box: `placed` is false until a layout gives it a place (autoLayout places the rest). */
export type GNode = { id: string; type: NodeType; spec: NodeSpec; x: number; y: number; placed?: boolean };
export type LoopWire = { max: number | string; when?: string; carry?: string };
export type GEdge = {
  from: string;
  to: string;
  kind: "sequence" | "parallel" | "loop" | "branch" | "each";
  label?: string;
  loop?: LoopWire;
};
export type Graph = {
  name: string;
  type: string | null;
  start: string;
  loose: string[];
  nodes: GNode[];
  edges: GEdge[];
  /** The bundles an `each` box opens, loaded by name when opened. */
  bundles: Record<string, Graph>;
};

export function emptyGraph(name: string): Graph {
  return { name, type: null, start: "", loose: [], nodes: [], edges: [], bundles: {} };
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);

/** A loop's wire data (the `loop:` of a node, or an `on_fail:` object), or null when `v` is not one. */
function loopOf(v: unknown): { to: string; wire: LoopWire } | null {
  if (!isRecord(v) || typeof v.to !== "string") return null;
  const wire: LoopWire = { max: typeof v.max === "number" || typeof v.max === "string" ? v.max : "" };
  if (str(v.when)) wire.when = str(v.when);
  if (str(v.carry)) wire.carry = str(v.carry);
  return { to: v.to, wire };
}

/** The parallel children of a workflow: nodes the composer reaches only through their parent. */
function childrenOf(nodes: Record<string, NodeSpec>): Set<string> {
  const out = new Set<string>();
  for (const n of Object.values(nodes)) if (n.type === "parallel" && Array.isArray(n.nodes)) for (const c of n.nodes) if (typeof c === "string") out.add(c);
  return out;
}

/** The boxes and wires of one workflow. Wires: a node's `next`, else the next node in the file that is not a
 * parallel child (a sequence wire; none after a branch with a `*` default); a wire per `branch` value; a loop wire
 * for `loop` and an `on_fail` loop; a parallel node's wires to each child and from each child to what follows the
 * parallel node. An `each` node is one box naming its bundle. */
export function fromWorkflow(name: string, spec: WorkflowSpec, layout: Layout = {}): Graph {
  const ids = Object.keys(spec.nodes);
  const children = childrenOf(spec.nodes);
  const known = new Set(ids);
  const after = (id: string): string | undefined => {
    const next = str(spec.nodes[id].next);
    if (next !== undefined) return next;
    return ids.slice(ids.indexOf(id) + 1).find((n) => !children.has(n));
  };

  const nodes: GNode[] = ids.map((id) => {
    const { type, ...rest } = spec.nodes[id];
    const at = layout[id];
    return { id, type, spec: rest, x: at?.x ?? 0, y: at?.y ?? 0, placed: at !== undefined };
  });

  const edges: GEdge[] = [];
  for (const id of ids) {
    const n = spec.nodes[id];
    const branch = isRecord(n.branch) ? n.branch : null;
    for (const [value, to] of Object.entries(branch ?? {})) if (typeof to === "string") edges.push({ from: id, to, kind: "branch", label: value });
    for (const l of [loopOf(n.loop), loopOf(n.on_fail)]) if (l) edges.push({ from: id, to: l.to, kind: "loop", loop: l.wire });
    if (children.has(id)) continue; // a child's way on is its parent's
    const next = after(id);
    if (n.type === "parallel") {
      const kids = (Array.isArray(n.nodes) ? n.nodes : []).filter((c): c is string => typeof c === "string");
      for (const c of kids) edges.push({ from: id, to: c, kind: "parallel" });
      if (next !== undefined) for (const c of kids) if (known.has(c)) edges.push({ from: c, to: next, kind: "parallel" });
    } else if (next !== undefined && !(branch && "*" in branch)) edges.push({ from: id, to: next, kind: "sequence" });
  }
  return {
    name,
    type: spec.type ?? null,
    start: spec.start,
    loose: [...(spec.loose ?? [])],
    nodes,
    edges,
    bundles: {},
  };
}
