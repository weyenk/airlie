// A workflow as the builder draws it (schema v2): boxes, a node each, and lines, a `to` entry each. Pure: no Vue,
// so the model tests fast. The YAML model is airlie.flow.spec (WorkflowSpec, NodeSpec, Line); a node is pure
// configuration, and a line carries the artifacts its source writes to the node it names.
//
// A box's fields hold everything but its type and its lines: the lines are the graph's edges, written back as each
// source's `to` list. What a line carries is never stored: it is the source's output names the target lists in
// `inputs`, worked out again after every edit (`relabel`). The boxes' order is the file's order (a new box goes
// last), not where they sit: a drag changes only the layout.

export const NODE_TYPES = ["ask", "conversation", "write", "meeting", "team", "check", "gate", "bundle"] as const;
export type NodeType = (typeof NODE_TYPES)[number];
export const isNodeType = (t: unknown): t is NodeType => typeof t === "string" && (NODE_TYPES as readonly string[]).includes(t);

/** The output formats a node may declare (airlie.flow.spec `Format`). */
export const FORMATS = ["json", "markdown", "text", "bool", "list", "ref:branch", "ref:worktree", "ref:pr", "file", "session"] as const;

/** The kinds a structured output's field may name (airlie.flow.outputs: `text | number | bool | list of <kind> |
 * one of a | b`, a trailing `?` for optional). */
export const KINDS = ["text", "number", "bool", "list of …", "one of …"] as const;

/** An output's declaration: a format, and for a structured answer its fields; `transient` outputs are recomputed
 * every run, never saved. In the YAML a bare format string says the same as `{format}`. */
export type OutputSpec = { format: string; fields?: Record<string, string>; transient?: boolean };

/** A node's fields as the YAML has them, its `type` and `to` aside (the box and its lines carry those). */
export type NodeSpec = Record<string, unknown>;
export type Layout = Record<string, { x: number; y: number }>;

/** A loop line's bound: a count or a config path, and the output handed back as `feedback.<target>`. */
export type LoopBound = { max: number | string; carry?: string };
/** A `to` entry as the YAML writes it: a bare node name, or the node with a condition and a loop bound. */
export type Line = string | { node: string; when?: string; loop?: LoopBound };

/** One workflow's mapping in the YAML, its layout aside. */
export type WorkflowSpec = {
  type?: string | null;
  inputs: string[];
  outputs: string[];
  nodes: Record<string, NodeSpec & { type: NodeType }>;
};

/** A box: `placed` is false until a layout gives it a place (autoLayout places the rest). */
export type GNode = { id: string; type: NodeType; spec: NodeSpec; x: number; y: number; placed?: boolean };
/** A line: `artifacts` are what it carries (none: an ordering line), `when` its condition, `loop` its bound when
 * it is drawn back to an earlier node. */
export type GEdge = { from: string; to: string; artifacts: string[]; when?: string; loop?: LoopBound };
export type Graph = {
  name: string;
  type: string | null;
  /** What the caller supplies (card, repo; a bundle's inputs). */
  inputs: string[];
  /** What a bundle hands back. */
  outputs: string[];
  nodes: GNode[];
  edges: GEdge[];
  /** The bundles a bundle box runs, loaded by name when opened. */
  bundles: Record<string, Graph>;
};

/** What a line drawn between two boxes may carry and say. */
export type LineOpts = { artifacts?: string[]; when?: string; loop?: LoopBound };

/** The inputs a new canvas starts with: what the shipped story workflow is handed. */
export const NEW_INPUTS: readonly string[] = ["card", "repo"];

export function emptyGraph(name: string): Graph {
  return { name, type: null, inputs: [...NEW_INPUTS], outputs: [], nodes: [], edges: [], bundles: {} };
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
/** A deep copy of a node's fields: YAML data, so JSON's copy is exact (and it reads through a Vue proxy, which
 * structuredClone refuses). */
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

/** The output names a box's fields declare, in their order. */
export const outputsOf = (spec: NodeSpec): string[] => (isRecord(spec.outputs) ? Object.keys(spec.outputs) : []);
/** The names a box's fields list in `inputs`. */
export const inputsOf = (spec: NodeSpec): string[] => strings(spec.inputs);

/** A `to` entry as a line (its artifacts worked out later), or null when it names no node. */
export function lineOf(from: string, entry: unknown): GEdge | null {
  if (typeof entry === "string") return { from, to: entry, artifacts: [] };
  if (!isRecord(entry) || typeof entry.node !== "string") return null;
  const edge: GEdge = { from, to: entry.node, artifacts: [] };
  if (typeof entry.when === "string") edge.when = entry.when;
  // a loop without its max is no loop (parseWorkflow refuses one): an empty max is never drawn or written
  const max = isRecord(entry.loop) ? entry.loop.max : undefined;
  if (isRecord(entry.loop) && hasMax(max)) {
    const loop: LoopBound = { max };
    if (typeof entry.loop.carry === "string") loop.carry = entry.loop.carry;
    edge.loop = loop;
  }
  return edge;
}

/** A loop bound's max is a count or a non-empty config path. */
export const hasMax = (max: unknown): max is number | string => typeof max === "number" || (typeof max === "string" && max.trim() !== "");

/** A line as the YAML writes it: a bare name with neither a condition nor a bound. */
function entryOf(edge: GEdge): Line {
  const loop = edge.loop && hasMax(edge.loop.max) ? edge.loop : undefined;
  if (edge.when === undefined && loop === undefined) return edge.to;
  const entry: Exclude<Line, string> = { node: edge.to };
  if (edge.when !== undefined) entry.when = edge.when;
  if (loop !== undefined) entry.loop = loop.carry !== undefined ? { max: loop.max, carry: loop.carry } : { max: loop.max };
  return entry;
}

/** What a line from `from` to `to` carries: `from`'s output names that `to` reads, in `from`'s order. */
function carried(from: GNode | undefined, to: GNode | undefined): string[] {
  if (!from || !to) return [];
  const reads = new Set(inputsOf(to.spec));
  return outputsOf(from.spec).filter((o) => reads.has(o));
}

/** The graph with every line's artifacts worked out again from its ends' fields. */
function relabel(graph: Graph): Graph {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  return { ...graph, edges: graph.edges.map((e) => ({ ...e, artifacts: carried(byId.get(e.from), byId.get(e.to)) })) };
}

/** The boxes and lines of one workflow: a box per node in the file's order, a line per `to` entry. */
export function fromWorkflow(name: string, spec: WorkflowSpec, layout: Layout = {}): Graph {
  const edges: GEdge[] = [];
  const nodes: GNode[] = Object.entries(spec.nodes).map(([id, n]) => {
    const { type, to, ...rest } = n;
    for (const entry of Array.isArray(to) ? to : []) {
      const edge = lineOf(id, entry);
      if (edge) edges.push(edge);
    }
    const at = layout[id];
    return { id, type, spec: rest, x: at?.x ?? 0, y: at?.y ?? 0, placed: at !== undefined };
  });
  return relabel({
    name,
    type: spec.type ?? null,
    inputs: [...(spec.inputs ?? [])],
    outputs: [...(spec.outputs ?? [])],
    nodes,
    edges,
    bundles: {},
  });
}

/** The workflow a graph is, as the YAML would hold it: `type` (when it serves one), its inputs and outputs, the
 * nodes in the graph's order with their lines last, and where each box sits. */
export function toWorkflow(graph: Graph): { spec: WorkflowSpec; layout: Layout } {
  const nodes: WorkflowSpec["nodes"] = {};
  const layout: Layout = {};
  for (const n of graph.nodes) {
    const to = graph.edges.filter((e) => e.from === n.id).map(entryOf);
    nodes[n.id] = { type: n.type, ...n.spec, ...(to.length ? { to } : {}) };
    layout[n.id] = { x: n.x, y: n.y };
  }
  return {
    spec: { ...(graph.type ? { type: graph.type } : {}), inputs: [...graph.inputs], outputs: [...graph.outputs], nodes },
    layout,
  };
}

/** A copy of the boxes in `ids` with their fields changed by `edit` (which may change them in place). */
function editing(graph: Graph, ids: string[], edit: (spec: NodeSpec, id: string) => void): Graph {
  const want = new Set(ids);
  return {
    ...graph,
    nodes: graph.nodes.map((n) => {
      if (!want.has(n.id)) return n;
      const spec = clone(n.spec);
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

/** The smallest free id `<type>-N`. */
export function freeId(graph: Graph, type: string): string {
  const taken = new Set(graph.nodes.map((n) => n.id));
  let i = 1;
  while (taken.has(`${type}-${i}`)) i += 1;
  return `${type}-${i}`;
}

/** The fields a new box of each type starts with: the ones its type needs, blank, so validation names them. A
 * meeting's seats and inputs come from the catalog, which the caller hands in as `spec`. */
function defaults(type: NodeType): NodeSpec {
  switch (type) {
    case "ask":
    case "conversation":
    case "write":
      return { role: "", prompt: "", inputs: [], outputs: {} };
    case "meeting":
      return { role: [], prompt: "", inputs: [], outputs: {} };
    case "team":
      return { role: { supervisor: "", worker: "" }, prompt: "", worker: "", inputs: [], outputs: {} };
    case "check":
    case "gate":
      return { run: "", inputs: [], outputs: {} };
    case "bundle":
      return { workflow: "", inputs: [], outputs: {} };
  }
}

/** The graph with a new box of `type` at (x, y), last in the file, with its type's blank fields and any `spec`
 * given. No line is drawn: a box with no line in starts once its inputs exist. */
export function addNode(graph: Graph, type: NodeType, x: number, y: number, spec: NodeSpec = {}): Graph {
  const id = freeId(graph, type);
  const node: GNode = { id, type, spec: { ...defaults(type), ...clone(spec) }, x, y, placed: true };
  return relabel({ ...graph, nodes: [...graph.nodes, node] });
}

/** The graph with box `id` at (x, y). Only the layout changes. */
export function moveNode(graph: Graph, id: string, x: number, y: number): Graph {
  return { ...graph, nodes: graph.nodes.map((n) => (n.id === id ? { ...n, x, y, placed: true } : n)), edges: graph.edges.map((e) => ({ ...e })) };
}

/** The graph with box `id`'s fields replaced by `spec`; the lines' artifacts follow its inputs and outputs. */
export function updateNode(graph: Graph, id: string, spec: NodeSpec): Graph {
  nodeOf(graph, id);
  const rest = clone(spec);
  delete rest.type; // the box's, not a field
  delete rest.to; // the lines'
  return relabel({ ...graph, nodes: graph.nodes.map((n) => (n.id === id ? { ...n, spec: rest } : n)) });
}

/** The graph without box `id`: every line into or out of it goes, and so does a `feedback.<id>` input. */
export function removeNode(graph: Graph, id: string): Graph {
  const rest = graph.nodes.filter((n) => n.id !== id);
  const g = editing({ ...graph, nodes: rest }, rest.map((n) => n.id), (s) => {
    if (Array.isArray(s.inputs)) s.inputs = s.inputs.filter((i) => i !== `feedback.${id}`);
  });
  return relabel({ ...g, edges: graph.edges.filter((e) => e.from !== id && e.to !== id) });
}

/** The graph with box `id` called `to`: its lines, and a `feedback.<id>` input, follow. */
export function renameNode(graph: Graph, id: string, to: string): Graph {
  if (id === to) return graph;
  if (!to.trim()) throw new Error("a box needs a name");
  if (graph.nodes.some((n) => n.id === to)) throw new Error(`a box is already called ${to}`);
  nodeOf(graph, id);
  const g = editing(graph, graph.nodes.map((n) => n.id), (s) => {
    if (Array.isArray(s.inputs)) s.inputs = s.inputs.map((i) => (i === `feedback.${id}` ? `feedback.${to}` : i));
  });
  const follow = (n: string) => (n === id ? to : n);
  return relabel({
    ...g,
    nodes: g.nodes.map((n) => (n.id === id ? { ...n, id: to } : n)),
    edges: g.edges.map((e) => ({ ...e, from: follow(e.from), to: follow(e.to) })),
  });
}

/** The graph with a line `from` → `to`: each of `artifacts` (outputs of `from`) joins the target's inputs if it
 * is not there yet, and `when` and `loop` say when the line is taken and its bound. A line already between the two
 * boxes is replaced in place. Throws on a box that is not there, an artifact `from` does not write, or a loop
 * without its max. */
export function connect(graph: Graph, from: string, to: string, opts: LineOpts = {}): Graph {
  const source = nodeOf(graph, from);
  nodeOf(graph, to);
  const artifacts = opts.artifacts ?? [];
  const writes = outputsOf(source.spec);
  for (const a of artifacts) if (!writes.includes(a)) throw new Error(`${from} writes no ${a}`);
  if (opts.loop && !hasMax(opts.loop.max)) throw new Error("a loop needs its max");

  const edge: GEdge = { from, to, artifacts: [] };
  if (opts.when) edge.when = opts.when;
  if (opts.loop) edge.loop = opts.loop.carry ? { max: opts.loop.max, carry: opts.loop.carry } : { max: opts.loop.max };
  const at = graph.edges.findIndex((e) => e.from === from && e.to === to);
  const edges = at < 0 ? [...graph.edges, edge] : graph.edges.map((e, i) => (i === at ? edge : e));
  const g = editing(graph, [to], (s) => {
    const have = inputsOf(s);
    const add = artifacts.filter((a) => !have.includes(a));
    if (add.length) s.inputs = [...have, ...add];
  });
  return relabel({ ...g, edges });
}

const sameLine = (a: GEdge, b: GEdge) => a.from === b.from && a.to === b.to && a.when === b.when;

/** The graph without the line `edge`, and without the target's inputs that line alone carried: an artifact
 * another line still brings in, or a workflow input, stays. */
export function removeEdge(graph: Graph, edge: GEdge): Graph {
  const current = relabel(graph);
  let at = current.edges.findIndex((e) => sameLine(e, edge));
  if (at < 0) at = current.edges.findIndex((e) => e.from === edge.from && e.to === edge.to);
  if (at < 0) return graph;
  const gone = current.edges[at];
  const edges = current.edges.filter((_, i) => i !== at);
  const still = new Set([...graph.inputs, ...edges.filter((e) => e.to === gone.to).flatMap((e) => e.artifacts)]);
  const drop = new Set(gone.artifacts.filter((a) => !still.has(a)));
  const g = editing({ ...current, edges }, [gone.to], (s) => {
    if (drop.size && Array.isArray(s.inputs)) s.inputs = s.inputs.filter((i) => !(typeof i === "string" && drop.has(i)));
  });
  return relabel(g);
}

const PROMPT_FILE = "prompt:";
const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** `text` with every placeholder of `old` (`{old}`, `{old.field}`, `{?old}`, `{/old}`) naming `to` instead. */
function renamePlaceholders(text: string, old: string, to: string): string {
  return text.replace(new RegExp(`\\{([?/]?)${escape(old)}(?=[}.])`, "g"), (_m, mark: string) => `{${mark}${to}`);
}

/** The graph with box `id`'s output `old` called `to`, in place: the lines that carry it, every reader's inputs,
 * `session` and `each`, the `{old}` placeholders in a reader's own prompt text, a loop's `carry` from `id`, and the
 * workflow's declared outputs follow. A reader is a box a line from `id` brings it to, or, when `id` is its only
 * writer, any box that reads it. A reader whose prompt (or a team's worker) is a prompt file that names it cannot
 * be rewritten here: the rename is refused with those readers. `promptFields` are the fields of the prompt files
 * the caller knows (id -> fields); a prompt file not in it is taken to name the output. Throws when `id` has no
 * output `old` or has one called `to` already. */
export function renameOutput(graph: Graph, id: string, old: string, to: string, promptFields: Record<string, string[]> = {}): Graph | { refused: string[] } {
  const box = nodeOf(graph, id);
  const outputs = isRecord(box.spec.outputs) ? box.spec.outputs : {};
  if (!(old in outputs)) throw new Error(`${id} has no output ${old}`);
  if (old === to) return graph;
  if (!to.trim()) throw new Error("an output needs a name");
  if (to in outputs) throw new Error(`${id} already has an output ${to}`);

  // a box reads `old` by its inputs, or by the session it continues or the list a bundle runs over
  const reads = (n: GNode) => inputsOf(n.spec).includes(old) || n.spec.session === old || n.spec.each === old;
  const lined = new Set(graph.edges.filter((e) => e.from === id).map((e) => e.to));
  const only = !graph.nodes.some((n) => n.id !== id && outputsOf(n.spec).includes(old));
  const readers = graph.nodes.filter((n) => n.id !== id && reads(n) && (lined.has(n.id) || only));

  const names = (text: unknown) => {
    if (typeof text !== "string" || !text.startsWith(PROMPT_FILE)) return false;
    const fields = promptFields[text.slice(PROMPT_FILE.length)];
    return fields === undefined || fields.includes(old);
  };
  const refused = readers.filter((n) => names(n.spec.prompt) || names(n.spec.worker)).map((n) => n.id);
  if (refused.length) return { refused };

  let g = editing(graph, [id], (s) => {
    s.outputs = Object.fromEntries(Object.entries(outputs).map(([k, v]) => [k === old ? to : k, v]));
  });
  g = editing(g, readers.map((n) => n.id), (s) => {
    if (Array.isArray(s.inputs)) s.inputs = s.inputs.map((i) => (i === old ? to : i));
    for (const key of ["session", "each"]) if (s[key] === old) s[key] = to;
    for (const key of ["prompt", "turn", "worker"]) {
      const text = s[key];
      if (typeof text === "string" && !text.startsWith(PROMPT_FILE)) s[key] = renamePlaceholders(text, old, to);
    }
  });
  return relabel({
    ...g,
    outputs: g.outputs.map((o) => (o === old ? to : o)),
    edges: g.edges.map((e) => (e.from === id && e.loop?.carry === old ? { ...e, loop: { ...e.loop, carry: to } } : e)),
  });
}

/** The names box `id` may read: the workflow's inputs, the outputs of every other box, and `feedback.<id>` when a
 * loop line into it carries something back. */
export function readable(graph: Graph, id: string): string[] {
  const names = new Set<string>(graph.inputs);
  for (const n of graph.nodes) if (n.id !== id) for (const o of outputsOf(n.spec)) names.add(o);
  if (graph.edges.some((e) => e.to === id && e.loop?.carry)) names.add(`feedback.${id}`);
  return [...names];
}
