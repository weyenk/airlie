// A workflow as the builder draws it: boxes (a node each) and wires (the moves between them). Pure: no Vue, so the
// model tests fast. The YAML model is airlie.flow.spec (WorkflowSpec, NodeSpec); the wires are derived the way the
// composer walks it (airlie.flow.composer `_route`).
//
// The boxes' order is the file's order (the composer's dict order), not where they sit: the YAML has no way to say
// "stop here" but being last, so a box dragged across the canvas must not change what follows what. Every edit
// below changes the node fields its move means and draws the wires again from them (`rewire`).

export const NODE_TYPES = ["ask", "conversation", "write", "meeting", "check", "gate", "parallel", "each"] as const;
export type NodeType = (typeof NODE_TYPES)[number];
export const isNodeType = (t: unknown): t is NodeType => typeof t === "string" && (NODE_TYPES as readonly string[]).includes(t);

/** The output formats a node may declare (airlie.flow.spec `Format`). */
export const FORMATS = ["json", "markdown", "text", "bool", "list", "ref:branch", "ref:worktree", "ref:pr", "file", "session"] as const;

/** A node's fields as the YAML has them, `type` aside (the box carries it). */
export type NodeSpec = Record<string, unknown>;
export type Layout = Record<string, { x: number; y: number }>;

/** One workflow's mapping in the YAML, its layout aside. `start` is absent until the canvas has a box. */
export type WorkflowSpec = {
  type?: string | null;
  start?: string;
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

/** The five moves a wire drawn between two boxes may be, and the fields each needs. */
export type Move = "sequence" | "parallel" | "loop" | "branch" | "each";
export const MOVES: readonly Move[] = ["sequence", "parallel", "loop", "branch", "each"];
export type MoveOpts = { loop?: LoopWire; branch?: { on: string; value: string }; each?: { workflow: string } };

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

const kidsOf = (n: NodeSpec): string[] => (Array.isArray(n.nodes) ? n.nodes.filter((c): c is string => typeof c === "string") : []);

/** The parallel children of a workflow: nodes the composer reaches only through their parent. */
function childrenOf(nodes: Iterable<NodeSpec & { type?: unknown }>): Set<string> {
  const out = new Set<string>();
  for (const n of nodes) if (n.type === "parallel") for (const c of kidsOf(n)) out.add(c);
  return out;
}

/** The branch a node routes by: only with `on` set does the composer read it. */
const branchOf = (n: NodeSpec): Record<string, unknown> | null => (isRecord(n.branch) && typeof n.on === "string" && n.on ? n.branch : null);

/** The boxes and wires of one workflow. Wires: a node's `next`, else the next node in the file that is not a
 * parallel child (a sequence wire; none after a branch with a `*` default); a wire per `branch` value (when `on`
 * names the output it reads); a loop wire for `loop` and an `on_fail` loop; a parallel node's wires to each child
 * and from each child to what follows the parallel node. An `each` node is one box naming its bundle. */
export function fromWorkflow(name: string, spec: WorkflowSpec, layout: Layout = {}): Graph {
  const ids = Object.keys(spec.nodes);
  const children = childrenOf(Object.values(spec.nodes));
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
    const branch = branchOf(n);
    for (const [value, to] of Object.entries(branch ?? {})) if (typeof to === "string") edges.push({ from: id, to, kind: "branch", label: value });
    for (const l of [loopOf(n.loop), loopOf(n.on_fail)]) if (l) edges.push({ from: id, to: l.to, kind: "loop", loop: l.wire });
    if (children.has(id)) continue; // a child's way on is its parent's
    const next = after(id);
    if (n.type === "parallel") {
      const kids = kidsOf(n);
      for (const c of kids) edges.push({ from: id, to: c, kind: "parallel" });
      if (next !== undefined) for (const c of kids) if (known.has(c)) edges.push({ from: c, to: next, kind: "parallel" });
    } else if (next !== undefined && !(branch && "*" in branch)) edges.push({ from: id, to: next, kind: "sequence" });
  }
  return {
    name,
    type: spec.type ?? null,
    start: spec.start ?? "",
    loose: [...(spec.loose ?? [])],
    nodes,
    edges,
    bundles: {},
  };
}

/** The workflow a graph is, as the YAML would hold it: `type` (when it serves one), `start` (once a box exists),
 * `loose` (when any), the nodes in the graph's order, and where each box sits. */
export function toWorkflow(graph: Graph): { spec: WorkflowSpec; layout: Layout } {
  const nodes: WorkflowSpec["nodes"] = {};
  const layout: Layout = {};
  for (const n of graph.nodes) {
    nodes[n.id] = { type: n.type, ...n.spec };
    layout[n.id] = { x: n.x, y: n.y };
  }
  return {
    spec: {
      ...(graph.type ? { type: graph.type } : {}),
      ...(graph.start ? { start: graph.start } : {}),
      ...(graph.loose.length ? { loose: [...graph.loose] } : {}),
      nodes,
    },
    layout,
  };
}

/** The graph with its wires drawn again from its nodes' fields; boxes keep their places. */
function rewire(graph: Graph): Graph {
  const { spec, layout } = toWorkflow(graph);
  const drawn = fromWorkflow(graph.name, spec, layout);
  const placed = new Map(graph.nodes.map((n) => [n.id, n.placed]));
  return { ...graph, nodes: drawn.nodes.map((n) => ({ ...n, placed: placed.get(n.id) })), edges: drawn.edges };
}

/** A copy of the boxes with `id`'s fields changed by `edit` (which may change them in place). */
function editing(graph: Graph, ids: string[], edit: (spec: NodeSpec, id: string) => void): Graph {
  const want = new Set(ids);
  return {
    ...graph,
    nodes: graph.nodes.map((n) => {
      if (!want.has(n.id)) return n;
      const spec = structuredClone(n.spec);
      edit(spec, n.id);
      return { ...n, spec };
    }),
  };
}

function nodeOf(graph: Graph, id: string): GNode {
  const n = graph.nodes.find((x) => x.id === id);
  if (!n) throw new Error(`no box ${id}`);
  return n;
}

/** The box `id` falls through to: the next one in the graph's order that is not a parallel child. */
function fallThrough(graph: Graph, id: string): string | undefined {
  const ids = graph.nodes.map((n) => n.id);
  const children = childrenOf(graph.nodes.map((n) => ({ ...n.spec, type: n.type })));
  return ids.slice(ids.indexOf(id) + 1).find((n) => !children.has(n));
}

/** Where the composer goes after `id` when no branch or loop decides: its `next`, else the fall-through. */
const onward = (graph: Graph, id: string): string | undefined => str(nodeOf(graph, id).spec.next) ?? fallThrough(graph, id);

/** `id`'s way on set to `to`: no `next` when `to` is what it falls through to anyway. */
function pointTo(graph: Graph, id: string, to: string | undefined): Graph {
  const plain = fallThrough(graph, id) === to;
  return editing(graph, [id], (s) => {
    if (plain || to === undefined) delete s.next;
    else s.next = to;
  });
}

/** The smallest free id `<type>-N`. */
export function freeId(graph: Graph, type: string): string {
  const taken = new Set(graph.nodes.map((n) => n.id));
  let i = 1;
  while (taken.has(`${type}-${i}`)) i += 1;
  return `${type}-${i}`;
}

/** The fields a new box of each type starts with: the ones its type needs, blank, so validation names them. */
function defaults(type: NodeType): NodeSpec {
  switch (type) {
    case "ask":
    case "conversation":
    case "write":
      return { role: "", prompt: "", outputs: {} };
    case "meeting":
      return { role: [], run: "" };
    case "check":
    case "gate":
      return { run: "" };
    case "parallel":
      return { nodes: [] };
    case "each":
      return { over: "", workflow: "" };
  }
}

/** The graph with a new box of `type` at (x, y), last in the file (the box that was last now falls through to
 * it), with its type's blank fields and any `spec` given. The first box on a canvas is its start. */
export function addNode(graph: Graph, type: NodeType, x: number, y: number, spec: NodeSpec = {}): Graph {
  const id = freeId(graph, type);
  const node: GNode = { id, type, spec: { ...defaults(type), ...spec }, x, y, placed: true };
  return rewire({ ...graph, start: graph.start || id, nodes: [...graph.nodes, node] });
}

/** The graph with box `id` at (x, y). Only the layout changes: what follows what is the file's order. */
export function moveNode(graph: Graph, id: string, x: number, y: number): Graph {
  return { ...graph, nodes: graph.nodes.map((n) => (n.id === id ? { ...n, x, y, placed: true } : n)), edges: graph.edges.map((e) => ({ ...e })) };
}

/** The graph with box `id`'s fields replaced by `spec`. */
export function updateNode(graph: Graph, id: string, spec: NodeSpec): Graph {
  nodeOf(graph, id);
  return rewire({ ...graph, nodes: graph.nodes.map((n) => (n.id === id ? { ...n, spec: structuredClone(spec) } : n)) });
}

/** Every field of `spec` that names box `id`, renamed to `to` (or dropped when `to` is null). */
function retarget(spec: NodeSpec, id: string, to: string | null): void {
  if (spec.next === id) {
    if (to === null) delete spec.next;
    else spec.next = to;
  }
  if (isRecord(spec.branch)) {
    for (const [value, target] of Object.entries(spec.branch)) {
      if (target !== id) continue;
      if (to === null) delete spec.branch[value];
      else spec.branch[value] = to;
    }
    if (!Object.keys(spec.branch).length) {
      delete spec.branch;
      delete spec.on;
    }
  }
  for (const key of ["loop", "on_fail"] as const) {
    const loop = spec[key];
    if (!isRecord(loop) || loop.to !== id) continue;
    if (to === null) delete spec[key];
    else loop.to = to;
  }
  if (Array.isArray(spec.nodes)) spec.nodes = to === null ? spec.nodes.filter((c) => c !== id) : spec.nodes.map((c) => (c === id ? to : c));
  if (to !== null && Array.isArray(spec.inputs)) spec.inputs = spec.inputs.map((i) => (i === `feedback.${id}` ? `feedback.${to}` : i));
}

/** The graph without box `id`: its wires go, and so does every field that named it. A start that was the box
 * moves to the first box left. */
export function removeNode(graph: Graph, id: string): Graph {
  const rest = graph.nodes.filter((n) => n.id !== id);
  const g = editing({ ...graph, nodes: rest }, rest.map((n) => n.id), (s) => retarget(s, id, null));
  return rewire({ ...g, start: graph.start === id ? (rest[0]?.id ?? "") : graph.start });
}

/** The graph with box `id` called `to`, every field that named it (and a `feedback.<id>` input) following. */
export function renameNode(graph: Graph, id: string, to: string): Graph {
  if (id === to) return graph;
  if (!to.trim()) throw new Error("a box needs a name");
  if (graph.nodes.some((n) => n.id === to)) throw new Error(`a box is already called ${to}`);
  nodeOf(graph, id);
  const g = editing(graph, graph.nodes.map((n) => n.id), (s) => retarget(s, id, to));
  return rewire({ ...g, start: graph.start === id ? to : graph.start, nodes: g.nodes.map((n) => (n.id === id ? { ...n, id: to } : n)) });
}

/** The graph with a wire `from` → `to` drawn as `move`:
 * - sequence: `next` on the source, unless `to` is what it falls through to;
 * - parallel: `to` joins the parallel box the source is, or the one it goes on to, else a new one placed after
 *   the source (in the file and on the canvas) that goes on to where the source went;
 * - loop: `loop: {to, max, when?, carry?}` on the source;
 * - branch: `on` and `branch[value]` on the source;
 * - each: the each box (the target, else the source) runs bundle `workflow`, and the source goes on to the target.
 * Throws when the move's fields are missing. */
export function connect(graph: Graph, from: string, to: string, move: Move, opts: MoveOpts = {}): Graph {
  const source = nodeOf(graph, from);
  const target = nodeOf(graph, to);
  switch (move) {
    case "sequence":
      return rewire(pointTo(graph, from, to));
    case "loop": {
      const l = opts.loop;
      if (!l || l.max === "" || l.max === undefined) throw new Error("a loop needs its max");
      const loop: Record<string, unknown> = { to, max: l.max };
      if (l.when) loop.when = l.when;
      if (l.carry) loop.carry = l.carry;
      return rewire(editing(graph, [from], (s) => void (s.loop = loop)));
    }
    case "branch": {
      const b = opts.branch;
      if (!b?.on) throw new Error("a branch needs the output it reads (on)");
      if (!b.value) throw new Error("a branch needs the value that takes this wire");
      return rewire(
        editing(graph, [from], (s) => {
          s.on = b.on;
          s.branch = { ...(isRecord(s.branch) ? s.branch : {}), [b.value]: to };
        }),
      );
    }
    case "each": {
      const workflow = opts.each?.workflow;
      if (!workflow) throw new Error("an each wire needs the bundle it runs");
      const box = target.type === "each" ? to : source.type === "each" ? from : null;
      if (box === null) throw new Error("an each wire needs an each box at one end");
      return rewire(pointTo(editing(graph, [box], (s) => void (s.workflow = workflow)), from, to));
    }
    case "parallel":
      return rewire(parallel(graph, source, to));
  }
}

function parallel(graph: Graph, source: GNode, to: string): Graph {
  const join = (g: Graph, p: string) => editing(g, [p], (s) => void (s.nodes = [...new Set([...kidsOf(s), to])]));
  if (source.type === "parallel") return join(graph, source.id);
  const went = onward(graph, source.id);
  if (went !== undefined && nodeOf(graph, went).type === "parallel" && went !== to) return join(graph, went);

  const id = freeId(graph, "parallel");
  const goOn = went !== to ? went : onward(graph, to);
  const x = source.x + 80;
  let y = source.y + 90;
  while (graph.nodes.some((n) => Math.abs(n.x - x) < 120 && Math.abs(n.y - y) < 56)) y += 90;
  const box: GNode = { id, type: "parallel", spec: { nodes: [to] }, x, y, placed: true };
  const at = graph.nodes.findIndex((n) => n.id === source.id) + 1;
  let g: Graph = { ...graph, nodes: [...graph.nodes.slice(0, at), box, ...graph.nodes.slice(at)] };
  g = pointTo(g, source.id, id);
  return pointTo(g, id, goOn === id ? undefined : goOn);
}

/** Whether `edge` comes from a field the builder can drop; a fall-through (the file's order) or a child's wire on
 * to what follows its parallel box does not. */
export function canRemove(graph: Graph, edge: GEdge): boolean {
  const n = graph.nodes.find((x) => x.id === edge.from);
  if (!n) return false;
  switch (edge.kind) {
    case "sequence":
      return n.spec.next === edge.to;
    case "parallel":
      return n.type === "parallel" && kidsOf(n.spec).includes(edge.to);
    case "loop":
      return loopOf(n.spec.loop)?.to === edge.to || loopOf(n.spec.on_fail)?.to === edge.to;
    case "branch":
      return isRecord(n.spec.branch) && edge.label !== undefined && n.spec.branch[edge.label] === edge.to;
    case "each":
      return false;
  }
}

/** The graph without the field that drew `edge`: the source's `next`, its branch value (and `on` with the last
 * one), its loop (or on_fail loop), or the parallel box's child. A wire no field draws is left as it is. */
export function removeEdge(graph: Graph, edge: GEdge): Graph {
  if (!canRemove(graph, edge)) return graph;
  return rewire(
    editing(graph, [edge.from], (s) => {
      if (edge.kind === "sequence") delete s.next;
      else if (edge.kind === "parallel") s.nodes = kidsOf(s).filter((c) => c !== edge.to);
      else if (edge.kind === "loop") {
        if (loopOf(s.loop)?.to === edge.to) delete s.loop;
        else delete s.on_fail;
      } else if (edge.kind === "branch" && isRecord(s.branch) && edge.label !== undefined) {
        delete s.branch[edge.label];
        if (!Object.keys(s.branch).length) {
          delete s.branch;
          delete s.on;
        }
      }
    }),
  );
}

/** The names a box may read: the outputs of every other box (a loop's carried `feedback.<to>` too) and the
 * workflow's loose names. */
export function readable(graph: Graph, id: string): string[] {
  const names = new Set<string>(graph.loose);
  for (const n of graph.nodes) {
    if (n.id !== id && isRecord(n.spec.outputs)) for (const k of Object.keys(n.spec.outputs)) names.add(k);
    for (const l of [loopOf(n.spec.loop), loopOf(n.spec.on_fail)]) if (l?.wire.carry) names.add(`feedback.${l.to}`);
  }
  return [...names];
}
