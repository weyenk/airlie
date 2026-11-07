import { mount } from "@vue/test-utils";
import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import Canvas from "./Canvas.vue";
import { fromWorkflow } from "./graph";
import type { Graph } from "./graph";
import { autoLayout } from "./layout";
import { parseWorkflow } from "./yaml";

const graphOf = (text: string): Graph => {
  const { name, spec, layout } = parseWorkflow(text);
  return autoLayout(fromWorkflow(name, spec, layout));
};
const mountCanvas = (graph: Graph, selected: string | null = null) => mount(Canvas, { props: { graph, selected, readonly: true } });

test("a box per node, placed where the graph says, in its type's shape", () => {
  const graph = graphOf(STORY_YAML);
  const w = mountCanvas(graph);
  expect(w.findAll("[data-box]")).toHaveLength(graph.nodes.length);
  const base = w.get("[data-box][data-node=base]");
  expect(base.attributes("style")).toContain("left: 184px"); // 160 + the 24 px margin
  expect(base.attributes("style")).toContain("width: 120px");
  expect(base.attributes("style")).toContain("height: 56px");
  const shape = (id: string) => w.get(`[data-box][data-node=${id}]`).attributes("data-shape");
  expect(shape("survey")).toBe("model");
  expect(shape("layers")).toBe("model");
  expect(shape("amigos")).toBe("model");
  expect(shape("intake")).toBe("code");
  expect(shape("review")).toBe("gate");
  expect(shape("plan_story")).toBe("parallel");
  expect(shape("build_layers")).toBe("each");
  expect(w.get("[data-box][data-node=build_layers]").text()).toContain("layer");
  expect(w.get("[data-box][data-node=plan_story]").text()).toContain("same time");
});

test("a wire per edge, each drawn for its kind", () => {
  const graph = graphOf(STORY_YAML);
  const w = mountCanvas(graph);
  expect(w.findAll("[data-edge]")).toHaveLength(graph.edges.length);
  expect(w.findAll("[data-edge=parallel]")).toHaveLength(4);
  expect(w.get("[data-edge=sequence][data-from=intake][data-to=base]").attributes("d")).toMatch(/^M 144 52 /); // base's right edge, mid height
});

test("a loop wire is dashed and runs below the boxes; a branch wire carries its value", () => {
  const loop = mountCanvas(graphOf(LAYER_YAML)).get("[data-edge=loop]");
  expect(loop.attributes("stroke-dasharray")).toBeTruthy();
  const ys = (loop.attributes("d") ?? "").match(/V (\d+)/)?.[1];
  expect(Number(ys)).toBeGreaterThan(24 + 56);
  const text = "workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a, on: v, branch: {yes: b}}\n      b: {type: check, run: x.b}\n";
  const w = mountCanvas(graphOf(text));
  expect(w.get("[data-edge-label]").text()).toBe("yes");
});

test("a click on a box selects it; the selected box shows pressed", async () => {
  const w = mountCanvas(graphOf(STORY_YAML), "base");
  expect(w.get("[data-node=base]").attributes("aria-pressed")).toBe("true");
  expect(w.get("[data-node=gate]").attributes("aria-pressed")).toBe("false");
  await w.get("[data-node=gate]").trigger("click");
  expect(w.emitted("select")).toEqual([["gate"]]);
});

test("an empty graph says how to start", () => {
  const empty: Graph = { name: "new", type: null, start: "", loose: [], nodes: [], edges: [], bundles: {} };
  expect(mountCanvas(empty).text()).toContain("No boxes yet");
});
