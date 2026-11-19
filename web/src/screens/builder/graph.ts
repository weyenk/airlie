// A workflow as the builder draws it (schema v2): boxes, a node each, and lines, an edge each. Pure: no Vue,
// so the model tests fast. The YAML model is airlie.workflow.spec (WorkflowSpec, NodeSpec, WireSpec); a node is
// pure configuration, and a line carries the artifacts its source writes to the node it names.
//
// A box's fields hold everything but its kind and its lines: the lines are the graph's edges, written back as the
// workflow's `edges` mapping. What a line carries is never stored: it is the source's output names the target
// lists in `inputs`, worked out again after every edit (`relabel`). The boxes' order is the file's order (a new
// box goes last), not where they sit: a drag changes only the layout.

export const NODE_KINDS = ["WORK", "ROUND_ROBIN", "SUPERVISOR_WORKERS", "K_INDEPENDENT_TEAMS"] as const;
export type NodeKind = (typeof NODE_KINDS)[number];
export const isNodeKind = (t: unknown): t is NodeKind => typeof t === "string" && (NODE_KINDS as readonly string[]).includes(t);

/** The output formats a node may declare (airlie.workflow.spec `Format`). */
export const FORMATS = ["json", "markdown", "text", "bool", "list", "ref:branch", "ref:worktree", "ref:pr", "file", "session"] as const;

/** The kinds a structured output's field may name. */
export const KINDS = ["text", "number", "bool", "list of …", "one of …"] as const;

/** An output's declaration: a format, and for a structured answer its fields; `transient` outputs are recomputed
 * every run, never saved. In the YAML a bare format string says the same as `{format}`. */
export type OutputSpec = { format: string; fields?: Record<string, string>; transient?: boolean };

/** A node's fields as the YAML has them, its `kind` aside (the box carries that). */
export type NodeSpec = Record<string, unknown>;
export type Layout = Record<string, { x: number; y: number }>;

/** A loop line's bound: a count or a config path, and the output handed back as `feedback.<target>`. */
export type LoopBound = { max: number | string; carry?: string };
/** An edge entry as the YAML writes it: a bare node name, or the node with a condition and a loop bound. */
export type Line = string | { target: string; when?: string; loop?: LoopBound };

/** One workflow's mapping in the YAML, its layout aside. */
export type WorkflowSpec = {
  title?: string | null;
  type?: string | null;
  inputs: string[];
  outputs: string[];
  nodes: Record<string, NodeSpec>;
  edges?: Record<string, Line[]>;
};

/** A box: `placed` is false until a layout gives it a place (autoLayout places the rest). */
export type GNode = { id: string; kind: NodeKind; spec: NodeSpec; x: number; y: number; placed?: boolean };
/** A line: `artifacts` are what it carries (none: an ordering line), `when` its condition, `loop` its bound when
 * it is drawn back to an earlier node. */
export type GEdge = { from: string; to: string; artifacts: string[]; when?: string; loop?: LoopBound };
/** What a workflow hands back as its declared outputs. */
export type Ports = { inputs: string[]; outputs: string[] };
export type Graph = {
  name: string;
  title: string | null;
  bundles?: Record<string, Ports>;
  /** What the caller supplies (card, repo). */
  inputs: string[];
  /** What the workflow hands back. */
  outputs: string[];
  nodes: GNode[];
  edges: GEdge[];
};

/** What a line drawn between two boxes may carry and say. */
export type LineOpts = { artifacts?: string[]; when?: string; loop?: LoopBound };

/** The inputs a new canvas starts with: what the shipped story workflow is handed. */
export const NEW_INPUTS: readonly string[] = ["card", "repo"];

export function emptyGraph(name: string): Graph {
  return { name, title: null, inputs: [...NEW_INPUTS], outputs: [], nodes: [], edges: [] };
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

/** An edge entry as a line (its artifacts worked out later), or null when it names no node. */
export function lineOf(from: string, entry: unknown): GEdge | null {
  if (typeof entry === "string") return { from, to: entry, artifacts: [] };
  if (!isRecord(entry) || typeof entry.target !== "string") return null;
  const edge: GEdge = { from, to: entry.target, artifacts: [] };
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
  const entry: Exclude<Line, string> = { target: edge.to };
  if (edge.when !== undefined) entry.when = edge.when;
  if (loop !== undefined) entry.loop = loop.carry !== undefined ? { max: loop.max, carry: loop.carry } : { max: loop.max };
  return entry;
}

/** What a line from `from` to `to` carries: `from`'s output names that `to` reads, in `from`'s order. */
function carried(graph: Graph, from: GNode | undefined, to: GNode | undefined): string[] {
  if (!from || !to) return [];
  const reads = new Set(inputsOf(to.spec));
  return outputsOfNode(graph, from.id).filter((o) => reads.has(o));
}

/** The graph with every line's artifacts worked out again from its ends' fields. */
function relabel(graph: Graph): Graph {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  return { ...graph, edges: graph.edges.map((e) => ({ ...e, artifacts: carried(graph, byId.get(e.from), byId.get(e.to)) })) };
}

/** The boxes and lines of one workflow: a box per node in the file's order, a line per edge entry. */
export function fromWorkflow(name: string, spec: WorkflowSpec, layout: Layout = {}): Graph {
  const nodes: GNode[] = Object.entries(spec.nodes).map(([id, n]) => {
    const { kind, to, ...rest } = n as any;
    const at = layout[id];
    return { id, kind: (kind ?? (n as any).type ?? "WORK") as NodeKind, spec: rest, x: at?.x ?? 0, y: at?.y ?? 0, placed: at !== undefined };
  });
  const edges: GEdge[] = [];
  // New schema: edges at workflow level
  for (const [from, lines] of Object.entries(spec.edges ?? {})) {
    for (const entry of lines) {
      const edge = lineOf(from, entry);
      if (edge) edges.push(edge);
    }
  }
  // Old schema compat: `to` on each node (tests still use this)
  for (const [id, n] of Object.entries(spec.nodes)) {
    const to = (n as any).to;
    if (Array.isArray(to)) {
      for (const entry of to) {
        // Old `to` entries use {node: ...} while new `edges` use {target: ...}
        const normalized = typeof entry === "string" ? entry : { target: (entry as any).node ?? (entry as any).target, ...((entry as any).when !== undefined ? { when: (entry as any).when } : {}), ...((entry as any).loop !== undefined ? { loop: (entry as any).loop } : {}) };
        const edge = lineOf(id, normalized);
        if (edge) edges.push(edge);
      }
    }
  }
  return relabel({
    name,
    title: spec.title ?? null,
    inputs: [...(spec.inputs ?? [])],
    outputs: [...(spec.outputs ?? [])],
    nodes,
    edges,
  });
}

/** The workflow a graph is, as the YAML would hold it: `title`, its inputs and outputs, the nodes in the graph's
 * order, the edges grouped by source, and where each box sits. */
export function toWorkflow(graph: Graph): { spec: WorkflowSpec; layout: Layout } {
  const nodes: WorkflowSpec["nodes"] = {};
  const edges: WorkflowSpec["edges"] = {};
  const layout: Layout = {};
  for (const n of graph.nodes) {
    nodes[n.id] = { kind: n.kind, ...n.spec };
    layout[n.id] = { x: n.x, y: n.y };
  }
  for (const e of graph.edges) {
    if (!edges[e.from]) edges[e.from] = [];
    edges[e.from].push(entryOf(e));
  }
  return {
    spec: { ...(graph.title ? { title: graph.title } : {}), inputs: [...graph.inputs], outputs: [...graph.outputs], nodes, edges },
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

/** The smallest free id `<kind>-N`. */
export function freeId(graph: Graph, kind: string): string {
  const taken = new Set(graph.nodes.map((n) => n.id));
  let i = 1;
  while (taken.has(`${kind}-${i}`)) i += 1;
  return `${kind}-${i}`;
}

/** The fields a new box of each kind starts with, blank, so validation names them. */
function defaults(kind: NodeKind): NodeSpec {
  switch (kind) {
    case "WORK":
      return { prompt: "", inputs: [], outputs: {} };
    case "ROUND_ROBIN":
      return { roles: [], prompt: "", agreement_when: "", inputs: [], outputs: {} };
    case "SUPERVISOR_WORKERS":
      return { supervisor: "", worker: "", workers: 3, prompt: "", inputs: [], outputs: {} };
    case "K_INDEPENDENT_TEAMS":
      return { teams: 3, roles: [""], prompt: "", inputs: [], outputs: {}, aggregate: "llm" };
  }
}

/** The graph with a new box of `kind` at (x, y), last in the file, with its kind's blank fields and any `spec`
 * given. No line is drawn: a box with no line in starts once its inputs exist. */
export function addNode(graph: Graph, kind: NodeKind, x: number, y: number, spec: NodeSpec = {}): Graph {
  const id = freeId(graph, kind.toLowerCase());
  const node: GNode = { id, kind, spec: { ...defaults(kind), ...clone(spec) }, x, y, placed: true };
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
  delete rest.kind; // the box's, not a field
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

/** The output names a box offers: its declared `outputs`. */
export function outputsOfNode(graph: Graph, id: string): string[] {
  const spec = graph.nodes.find((n) => n.id === id)?.spec ?? {};
  return outputsOf(spec);
}

/** Set the ports of bundle `name` on the graph (no-op in the four-kind model: kept for backward compat). */
export function withBundle(graph: Graph, name: string, ports: Ports): Graph {
  const bundles = { ...(graph.bundles ?? {}), [name]: portsOf(ports) };
  return relabel({ ...graph, bundles });
}

/** The names of the workflows the graph's bundle boxes run, each once. */
export const bundleNames = (_graph: Graph): string[] => [];
/** A workflow's ports: what it is handed and what it hands back. */
export const portsOf = (spec: { inputs?: string[]; outputs?: string[] }): Ports => ({ inputs: [...(spec.inputs ?? [])], outputs: [...(spec.outputs ?? [])] });

/** The graph with a line `from` → `to`: each of `artifacts` (outputs of `from`) joins the target's inputs if it
 * is not there yet, and `when` and `loop` say when the line is taken and its bound. A line already between the
 * two boxes is merged into, in place: the new artifacts join what it carries, and its `when` and `loop` stay
 * unless this call gives new ones. Throws on a box that is not there, an artifact `from` does not write, or a
 * loop without its max. */
export function connect(graph: Graph, from: string, to: string, opts: LineOpts = {}): Graph {
  nodeOf(graph, from);
  nodeOf(graph, to);
  const artifacts = opts.artifacts ?? [];
  const writes = outputsOfNode(graph, from);
  for (const a of artifacts) if (!writes.includes(a)) throw new Error(`${from} writes no ${a}`);
  if (opts.loop && !hasMax(opts.loop.max)) throw new Error("a loop needs its max");

  const at = graph.edges.findIndex((e) => e.from === from && e.to === to);
  const old = at < 0 ? undefined : graph.edges[at];
  const edge: GEdge = { from, to, artifacts: [] };
  const when = opts.when ? opts.when : old?.when;
  if (when) edge.when = when;
  const loop = opts.loop ?? old?.loop;
  if (loop) edge.loop = loop.carry ? { max: loop.max, carry: loop.carry } : { max: loop.max };
  const edges = at < 0 ? [...graph.edges, edge] : graph.edges.map((e, i) => (i === at ? edge : e));
  const g = editing(graph, [to], (s) => {
    const have = inputsOf(s);
    const add = artifacts.filter((a) => !have.includes(a));
    if (add.length) s.inputs = [...have, ...add];
  });
  return relabel({ ...g, edges });
}

/** The graph with the line `from` → `to` set to `patch`: it carries exactly `patch.artifacts` (none: an ordering
 * line), and says `when` and `loop` only if the patch does, so a patch without them clears them. A newly carried
 * artifact joins the target's inputs; one no longer carried leaves them unless another line or a workflow input
 * still brings it. Throws on a line that is not there, an artifact `from` does not write, or a loop without its max. */
export function updateEdge(graph: Graph, from: string, to: string, patch: { artifacts: string[]; when?: string; loop?: LoopBound }): Graph {
  const current = relabel(graph);
  const at = current.edges.findIndex((e) => e.from === from && e.to === to);
  if (at < 0) throw new Error(`no line from ${from} to ${to}`);
  const writes = outputsOfNode(graph, from);
  for (const a of patch.artifacts) if (!writes.includes(a)) throw new Error(`${from} writes no ${a}`);
  if (patch.loop && !hasMax(patch.loop.max)) throw new Error("a loop needs its max");

  const old = current.edges[at];
  const edge: GEdge = { from, to, artifacts: [] };
  if (patch.when) edge.when = patch.when;
  if (patch.loop) edge.loop = patch.loop.carry ? { max: patch.loop.max, carry: patch.loop.carry } : { max: patch.loop.max };
  const edges = graph.edges.map((e, i) => (i === at ? edge : e));
  const carried = new Set<string>(graph.inputs);
  graph.edges.forEach((e, i) => {
    if (i !== at && e.to === to) for (const a of e.artifacts) carried.add(a);
  });
  const g = editing(graph, [to], (s) => {
    const have = inputsOf(s);
    const leaving = old.artifacts.filter((a) => !patch.artifacts.includes(a) && !carried.has(a));
    const adding = patch.artifacts.filter((a) => !have.includes(a));
    if (leaving.length || adding.length) s.inputs = [...have.filter((i) => !leaving.includes(i)), ...adding];
  });
  return relabel({ ...g, edges });
}

/** The boxes that write `name` (other than `from` and `to`), and `name`'s readers: what a line from `from` to
 * `to` would lose by no longer carrying `name`. */
export function producersOf(graph: Graph, name: string, from: string, to: string): string[] {
  const boxes = graph.nodes.filter((n) => n.id !== from && n.id !== to && outputsOfNode(graph, n.id).includes(name)).map((n) => n.id);
  const readers = new Set<string>([to]);
  if (boxes.length) for (const id of graph.nodes.filter((n) => inputsOf(n.spec).includes(name)).map((n) => n.id)) readers.add(id);
  return [...readers];
}

/** The graph without the line `edge`: a no longer carried name leaves the target's inputs unless another line or
 * the workflow's inputs still bring it. */
export function removeEdge(graph: Graph, edge: GEdge): Graph {
  const edges = graph.edges.filter((e) => e !== edge);
  const leaving = edge.artifacts;
  const still = new Set(graph.inputs);
  for (const e of edges) if (e.to === edge.to) for (const a of e.artifacts) still.add(a);
  const g = editing(graph, [edge.to], (s) => {
    if (Array.isArray(s.inputs)) s.inputs = s.inputs.filter((i) => leaving.includes(i) ? still.has(i) : true);
  });
  return relabel({ ...g, edges });
}

const PROMPT_FILE = "prompt:";
const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** `text` with every placeholder of `old` (`{old}`, `{old.field}`, `{?old}`, `{/old}`) naming `to` instead. */
function renamePlaceholders(text: string, old: string, to: string): string {
  return text.replace(new RegExp(`\\{([?/]?)${escape(old)}(?=[}.])`, "g"), (_m, mark: string) => `{${mark}${to}`);
}

const CONDITION_WORDS = new Set(["and", "or", "not", "exists"]);
const CONDITION_TOKEN = /(==|!=|\(|\))|("[^"]*"|'[^']*')|([^\s()=!"']+)/g;
/** A line's condition with every path headed by `old` (`old`, `old.field`) headed by `to` instead. */
export function renameInCondition(when: string, old: string, to: string): string {
  let out = "";
  let last = 0;
  let afterCompare = false;
  for (const m of when.matchAll(CONDITION_TOKEN)) {
    out += when.slice(last, m.index);
    last = m.index + m[0].length;
    const word = m[3];
    const path = word !== undefined && !afterCompare && !CONDITION_WORDS.has(word);
    out += path && (word === old || word.startsWith(`${old}.`)) ? to + word.slice(old.length) : m[0];
    afterCompare = m[1] === "==" || m[1] === "!=";
  }
  return out + when.slice(last);
}
export function renameOutput(graph: Graph, id: string, old: string, to: string, promptFields: Record<string, string[]> = {}): Graph | { refused: string[] } {
  const box = nodeOf(graph, id);
  const outputs = isRecord(box.spec.outputs) ? box.spec.outputs : {};
  if (!(old in outputs)) throw new Error(`${id} has no output ${old}`);
  if (old === to) return graph;
  if (!to.trim()) throw new Error("an output needs a name");
  if (to in outputs) throw new Error(`${id} already has an output ${to}`);

  const reads = (n: GNode) => inputsOf(n.spec).includes(old);
  const lined = new Set(graph.edges.filter((e) => e.from === id).map((e) => e.to));
  const only = !graph.nodes.some((n) => n.id !== id && outputsOf(n.spec).includes(old));
  const readers = graph.nodes.filter((n) => n.id !== id && reads(n) && (lined.has(n.id) || only));

  const names = (text: unknown) => {
    if (typeof text !== "string" || !text.startsWith(PROMPT_FILE)) return false;
    const fields = promptFields[text.slice(PROMPT_FILE.length)];
    return fields === undefined || fields.includes(old);
  };
  const refused = readers.filter((n) => names(n.spec.prompt)).map((n) => n.id);
  if (refused.length) return { refused };

  let g = editing(graph, [id], (s) => {
    s.outputs = Object.fromEntries(Object.entries(outputs).map(([k, v]) => [k === old ? to : k, v]));
  });
  g = editing(g, readers.map((n) => n.id), (s) => {
    if (Array.isArray(s.inputs)) s.inputs = s.inputs.map((i) => (i === old ? to : i));
    for (const key of ["prompt", "worker"]) {
      const text = s[key];
      if (typeof text === "string" && !text.startsWith(PROMPT_FILE)) s[key] = renamePlaceholders(text, old, to);
    }
  });
  const conditioned = only ? null : new Set([id, ...readers.map((n) => n.id)]);
  return relabel({
    ...g,
    outputs: g.outputs.map((o) => (o === old ? to : o)),
    edges: g.edges.map((e) => {
      let next = e.from === id && e.loop?.carry === old ? { ...e, loop: { ...e.loop, carry: to } } : e;
      if (next.when !== undefined && (conditioned === null || conditioned.has(e.from))) {
        const when = renameInCondition(next.when, old, to);
        if (when !== next.when) next = { ...next, when };
      }
      return next;
    }),
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
