// The builder's YAML: read one workflow's file into the spec the graph is made from, and write a graph back as the
// file the canvas would save, shaped like the shipped config/workflows/*.yaml. Pure: no Vue.
import { Document, isMap, isScalar, parse, visit } from "yaml";
import type { YAMLMap } from "yaml";
import { isNodeType, toWorkflow } from "./graph";
import type { Graph, Layout, WorkflowSpec } from "./graph";

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** The one workflow under `workflows:` in `text`: its name, its spec (layout aside) and its layout. Throws when
 * the text does not parse, holds no `workflows:` mapping, holds other than one workflow, or a node has no type or
 * one the builder does not know. */
export function parseWorkflow(text: string): { name: string; spec: WorkflowSpec; layout: Layout } {
  const doc: unknown = parse(text);
  if (!isRecord(doc) || !isRecord(doc.workflows)) throw new Error("the text has no workflows: mapping");
  const names = Object.keys(doc.workflows);
  if (names.length !== 1) throw new Error(`the text must hold one workflow, not ${names.length}`);
  const name = names[0];
  const raw = doc.workflows[name];
  if (!isRecord(raw)) throw new Error(`workflow ${name} is not a mapping`);
  const { layout: rawLayout, nodes: rawNodes, ...rest } = raw;
  if (!isRecord(rawNodes)) throw new Error(`workflow ${name} has no nodes: mapping`);
  const nodes: WorkflowSpec["nodes"] = {};
  for (const [id, n] of Object.entries(rawNodes)) {
    if (!isRecord(n) || typeof n.type !== "string") throw new Error(`workflow ${name}: node ${id} has no type`);
    if (!isNodeType(n.type)) throw new Error(`workflow ${name}: node ${id} has unknown type '${n.type}'`);
    nodes[id] = { ...n, type: n.type };
  }
  const layout: Layout = {};
  for (const [id, at] of Object.entries(isRecord(rawLayout) ? rawLayout : {})) {
    if (isRecord(at) && typeof at.x === "number" && typeof at.y === "number") layout[id] = { x: at.x, y: at.y };
  }
  const spec: WorkflowSpec = { ...(rest as Omit<WorkflowSpec, "nodes">), start: typeof rest.start === "string" ? rest.start : "", nodes };
  return { name, spec, layout };
}

/** Node fields written on one line, as the shipped files write them. */
const FLOW_MAPS = new Set(["inputs", "outputs", "args", "loop", "on_fail", "branch"]);

/** The file the canvas would save: `type:`, `start` (once a box exists), `loose`, the nodes in the file's order
 * (what falls through to what; see graph.ts), and `layout:` last; lists, and the short maps a node carries, on
 * one line. */
export function toYaml(graph: Graph): string {
  const { spec, layout } = toWorkflow(graph);
  const wf: Record<string, unknown> = { ...spec, layout };

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
      if (key === "layout") entry.value.flow = true;
      else for (const field of entry.value.items) if (isScalar(field.key) && FLOW_MAPS.has(String(field.key.value)) && isMap(field.value)) field.value.flow = true;
    }
  }
  return doc.toString({ lineWidth: 0, flowCollectionPadding: false });
}
