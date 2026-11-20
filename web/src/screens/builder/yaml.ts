// The builder's YAML: read one workflow's file (schema v2) into the spec the graph is made from, and write a graph
// back as the file the canvas would save, shaped like the shipped config/workflows/*.yaml. Pure: no Vue.
import { Document, isMap, isScalar, parse, visit } from "yaml";
import type { YAMLMap } from "yaml";
import { hasMax, isNodeKind, toWorkflow } from "./graph";
import type { Graph, Layout, Line, WorkflowSpec } from "./graph";

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

const WORKFLOW_FIELDS = new Set(["title", "type", "inputs", "outputs", "nodes", "edges", "layout"]);

const names = (v: unknown, what: string): string[] => {
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v) || !v.every((x) => typeof x === "string")) throw new Error(`${what} must be a list of names`);
  return v;
};

/** The one workflow under `workflows:` in `text`: its name, its spec (layout aside) and its layout. Throws when
 * the text does not parse, holds no `workflows:` mapping, holds other than one workflow, has a field the schema
 * does not know, a node with no kind or one the builder does not know, or an edge that names no node. */
export function parseWorkflow(text: string): { name: string; spec: WorkflowSpec; layout: Layout } {
  const doc: unknown = parse(text);
  if (!isRecord(doc) || !isRecord(doc.workflows)) throw new Error("the text has no workflows: mapping");
  const wnames = Object.keys(doc.workflows);
  if (wnames.length !== 1) throw new Error(`the text must hold one workflow, not ${wnames.length}`);
  const name = wnames[0];
  const raw = doc.workflows[name];
  if (!isRecord(raw)) throw new Error(`workflow ${name} is not a mapping`);
  const w = `workflow ${name}`;
  for (const f of Object.keys(raw)) if (!WORKFLOW_FIELDS.has(f)) throw new Error(`${w}: unknown field '${f}'`);
  if (!isRecord(raw.nodes)) throw new Error(`${w} has no nodes: mapping`);

  const nodes: WorkflowSpec["nodes"] = {};
  for (const [id, n] of Object.entries(raw.nodes)) {
    const at = `${w}: node ${id}`;
    if (!isRecord(n) || typeof n.kind !== "string") throw new Error(`${at} has no kind`);
    if (!isNodeKind(n.kind)) throw new Error(`${at} has unknown kind '${n.kind}'`);
    nodes[id] = { ...n, kind: n.kind };
  }
  const edges: WorkflowSpec["edges"] = {};
  if (isRecord(raw.edges)) {
    for (const [from, lines] of Object.entries(raw.edges)) {
      if (!Array.isArray(lines)) throw new Error(`${w}: edges.${from} must be a list`);
      const parsed: Line[] = [];
      for (const line of lines) {
        if (typeof line === "string") { parsed.push(line); continue; }
        if (!isRecord(line) || typeof line.target !== "string") throw new Error(`${w}: edge from ${from} needs a target`);
        if (line.loop !== undefined && !(isRecord(line.loop) && hasMax(line.loop.max))) throw new Error(`${w}: edge from ${from}: loop needs max`);
        parsed.push(line as unknown as Line);
      }
      edges[from] = parsed;
    }
  }
  const layout: Layout = {};
  for (const [id, at] of Object.entries(isRecord(raw.layout) ? raw.layout : {})) {
    if (isRecord(at) && typeof at.x === "number" && typeof at.y === "number") layout[id] = { x: at.x, y: at.y };
  }
  const title = typeof raw.title === "string" ? raw.title : null;
  const type = typeof raw.type === "string" ? raw.type : null;
  const spec: WorkflowSpec = {
    ...(title ? { title } : {}),
    ...(type ? { type } : {}),
    inputs: names(raw.inputs, `${w}: inputs`),
    outputs: names(raw.outputs, `${w}: outputs`),
    nodes,
    edges,
  };
  return { name, spec, layout };
}

/** Node fields that are maps written on one line, as the shipped files write them. */
const FLOW_MAPS = new Set(["outputs", "args", "events", "role"]);

/** The file the canvas would save: `title:`, the workflow's `inputs` and `outputs` (each when it has any), the
 * nodes in the file's order, the `edges` mapping, and `layout:` last. Lists go on one line; an edges list holding
 * a condition or a loop goes a line per entry. */
export function toYaml(graph: Graph): string {
  const { spec, layout } = toWorkflow(graph);
  const wf: Record<string, unknown> = {
    ...(spec.title ? { title: spec.title } : {}),
    ...(spec.type ? { type: spec.type } : {}),
    ...(spec.inputs.length ? { inputs: spec.inputs } : {}),
    ...(spec.outputs.length ? { outputs: spec.outputs } : {}),
    nodes: spec.nodes,
    ...(Object.keys(spec.edges ?? {}).length ? { edges: spec.edges! } : {}),
    layout,
  };

  const doc = new Document({ workflows: { [graph.name]: wf } });
  visit(doc, {
    Seq(_, seq) {
      seq.flow = true;
    },
  });
  const w = doc.getIn(["workflows", graph.name]) as YAMLMap;
  for (const key of ["nodes", "layout", "edges"]) {
    const map = w.get(key);
    if (!isMap(map)) continue;
    for (const entry of map.items) {
      if (!isMap(entry.value)) continue;
      if (key === "layout") {
        entry.value.flow = true;
        continue;
      }
      if (key === "edges") {
        // edges values are Seqs (list of edge entries), keep them flow for simple ones
        continue;
      }
      for (const field of entry.value.items) {
        if (!isScalar(field.key)) continue;
        const k = String(field.key.value);
        if (FLOW_MAPS.has(k) && isMap(field.value)) field.value.flow = true;
      }
    }
  }
  return doc.toString({ lineWidth: 0, flowCollectionPadding: false });
}
