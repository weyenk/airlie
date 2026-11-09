// The builder's YAML: read one workflow's file (schema v2) into the spec the graph is made from, and write a graph
// back as the file the canvas would save, shaped like the shipped config/workflows/*.yaml. Pure: no Vue.
import { Document, isMap, isScalar, isSeq, parse, visit } from "yaml";
import type { YAMLMap } from "yaml";
import { isNodeType, toWorkflow } from "./graph";
import type { Graph, Layout, WorkflowSpec } from "./graph";

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** The old schema's fields and types, which the server refuses (airlie.flow.spec `_OLD_*`), in its words. */
const OLD_NODE_FIELDS = ["parse", "after", "done", "when", "next", "on", "branch", "loop", "nodes", "over"];
const OLD_WORKFLOW_FIELDS = ["start", "loose"];
const OLD_TYPES = ["parallel", "each"];
const old = (what: string, then = "write a `to` line") => `${what} is the old schema; run the workflow upgrade or ${then}`;
const WORKFLOW_FIELDS = new Set(["type", "inputs", "outputs", "nodes", "layout"]);

const names = (v: unknown, what: string): string[] => {
  if (v === undefined || v === null) return [];
  if (!Array.isArray(v) || !v.every((x) => typeof x === "string")) throw new Error(`${what} must be a list of names`);
  return v;
};

/** The one workflow under `workflows:` in `text`: its name, its spec (layout aside) and its layout. Throws when
 * the text does not parse, holds no `workflows:` mapping, holds other than one workflow, uses the old schema
 * (start, loose, next, branch, loop, parallel, each, ...), has a field the schema does not know, a node with no
 * type or one the builder does not know, or a `to` line that names no node. */
export function parseWorkflow(text: string): { name: string; spec: WorkflowSpec; layout: Layout } {
  const doc: unknown = parse(text);
  if (!isRecord(doc) || !isRecord(doc.workflows)) throw new Error("the text has no workflows: mapping");
  const wnames = Object.keys(doc.workflows);
  if (wnames.length !== 1) throw new Error(`the text must hold one workflow, not ${wnames.length}`);
  const name = wnames[0];
  const raw = doc.workflows[name];
  if (!isRecord(raw)) throw new Error(`workflow ${name} is not a mapping`);
  const w = `workflow ${name}`;
  for (const f of OLD_WORKFLOW_FIELDS) if (f in raw) throw new Error(`${w}: ${old(`field '${f}'`)}`);
  for (const f of Object.keys(raw)) if (!WORKFLOW_FIELDS.has(f)) throw new Error(`${w}: unknown field '${f}'`);
  if (!isRecord(raw.nodes)) throw new Error(`${w} has no nodes: mapping`);

  const nodes: WorkflowSpec["nodes"] = {};
  for (const [id, n] of Object.entries(raw.nodes)) {
    const at = `${w}: node ${id}`;
    if (!isRecord(n) || typeof n.type !== "string") throw new Error(`${at} has no type`);
    if (OLD_TYPES.includes(n.type)) throw new Error(`${at}: ${old(`type '${n.type}'`)}`);
    if (!isNodeType(n.type)) throw new Error(`${at} has unknown type '${n.type}'`);
    for (const f of OLD_NODE_FIELDS) if (f in n) throw new Error(`${at}: ${old(`field '${f}'`)}`);
    if (isRecord(n.on_fail)) throw new Error(`${at}: ${old("field 'on_fail'", "draw a loop line")}`);
    if (n.to !== undefined) {
      if (!Array.isArray(n.to)) throw new Error(`${at}: to must be a list of lines`);
      for (const line of n.to) if (typeof line !== "string" && !(isRecord(line) && typeof line.node === "string")) throw new Error(`${at}: a to line needs a node`);
    }
    nodes[id] = { ...n, type: n.type };
  }
  const layout: Layout = {};
  for (const [id, at] of Object.entries(isRecord(raw.layout) ? raw.layout : {})) {
    if (isRecord(at) && typeof at.x === "number" && typeof at.y === "number") layout[id] = { x: at.x, y: at.y };
  }
  const type = typeof raw.type === "string" ? raw.type : null;
  const spec: WorkflowSpec = { ...(type ? { type } : {}), inputs: names(raw.inputs, `${w}: inputs`), outputs: names(raw.outputs, `${w}: outputs`), nodes };
  return { name, spec, layout };
}

/** Node fields that are maps written on one line, as the shipped files write them. */
const FLOW_MAPS = new Set(["outputs", "args", "events", "role"]);

/** The file the canvas would save: `type:`, the workflow's `inputs` and `outputs` (each when it has any), the nodes
 * in the file's order with their `to` lines last, and `layout:` last. Lists, and the short maps a node carries, go
 * on one line; a `to` list holding a condition or a loop goes a line per entry. */
export function toYaml(graph: Graph): string {
  const { spec, layout } = toWorkflow(graph);
  const wf: Record<string, unknown> = {
    ...(spec.type ? { type: spec.type } : {}),
    ...(spec.inputs.length ? { inputs: spec.inputs } : {}),
    ...(spec.outputs.length ? { outputs: spec.outputs } : {}),
    nodes: spec.nodes,
    layout,
  };

  const doc = new Document({ workflows: { [graph.name]: wf } });
  visit(doc, {
    Seq(_, seq) {
      seq.flow = true;
    },
  });
  const w = doc.getIn(["workflows", graph.name]) as YAMLMap;
  for (const key of ["nodes", "layout"]) {
    const map = w.get(key);
    if (!isMap(map)) continue;
    for (const entry of map.items) {
      if (!isMap(entry.value)) continue;
      if (key === "layout") {
        entry.value.flow = true;
        continue;
      }
      for (const field of entry.value.items) {
        if (!isScalar(field.key)) continue;
        const k = String(field.key.value);
        if (FLOW_MAPS.has(k) && isMap(field.value)) field.value.flow = true;
        if (k === "to" && isSeq(field.value) && field.value.items.some((i) => isMap(i))) {
          field.value.flow = false;
          for (const i of field.value.items) if (isMap(i)) i.flow = true;
        }
      }
    }
  }
  return doc.toString({ lineWidth: 0, flowCollectionPadding: false });
}
