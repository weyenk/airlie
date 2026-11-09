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
  expect(shape("layers")).toBe("code"); // a named catalog operation until Part 2
  expect(shape("amigos")).toBe("model");
  expect(shape("intake")).toBe("code");
  expect(shape("review")).toBe("gate");
  expect(shape("build_layers")).toBe("bundle");
  expect(w.get("[data-box][data-node=build_layers]").text()).toContain("layer");
});

test("a wire per line", () => {
  const graph = graphOf(STORY_YAML);
  const w = mountCanvas(graph);
  expect(w.findAll("[data-edge]")).toHaveLength(graph.edges.length);
  expect(w.get("[data-edge=line][data-from=intake][data-to=base]").attributes("d")).toMatch(/^M 144 52 /); // base's right edge, mid height
});

// replaced in Task 3 (ports and labelled lines): the branch half reads the old schema's on/branch wiring
test.skip("a loop wire is dashed and runs below the boxes; a branch wire carries its value", () => {
  const loop = mountCanvas(graphOf(LAYER_YAML)).get("[data-edge=loop]");
  expect(loop.attributes("stroke-dasharray")).toBeTruthy();
  const ys = (loop.attributes("d") ?? "").match(/V (\d+)/)?.[1];
  expect(Number(ys)).toBeGreaterThan(24 + 56);
  const text = "workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a, on: v, branch: {yes: b}}\n      b: {type: check, run: x.b}\n";
  const w = mountCanvas(graphOf(text));
  expect(w.get("[data-edge-label]").text()).toBe("yes");
});

test("a loop line is dashed and runs below the boxes; a line's condition shows on it", () => {
  const loop = mountCanvas(graphOf(LAYER_YAML)).get("[data-edge=loop]");
  expect(loop.attributes("stroke-dasharray")).toBeTruthy();
  const ys = (loop.attributes("d") ?? "").match(/V (\d+)/)?.[1];
  expect(Number(ys)).toBeGreaterThan(24 + 56);
  const text = "workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a, to: [{node: b, when: v == yes}]}\n      b: {type: check, run: x.b}\n";
  expect(mountCanvas(graphOf(text)).get("[data-edge-label]").text()).toBe("if v == yes");
});

test("a click on a box selects it; the selected box shows pressed", async () => {
  const w = mountCanvas(graphOf(STORY_YAML), "base");
  expect(w.get("[data-node=base]").attributes("aria-pressed")).toBe("true");
  expect(w.get("[data-node=gate]").attributes("aria-pressed")).toBe("false");
  await w.get("[data-node=gate]").trigger("click");
  expect(w.emitted("select")).toEqual([["gate"]]);
});

test("an empty graph says how to start", () => {
  const empty: Graph = { name: "new", type: null, inputs: [], outputs: [], nodes: [], edges: [], bundles: {} };
  expect(mountCanvas(empty).text()).toContain("No boxes yet");
});

// ---- editing (jsdom puts the canvas at 0,0, so a pointer's client point is its canvas point) ----

const editable = (graph: Graph, props: Record<string, unknown> = {}) => mount(Canvas, { props: { graph, selected: null, ...props }, attachTo: document.body });
const pointer = (type: string, x: number, y: number) => window.dispatchEvent(new PointerEvent(type, { clientX: x, clientY: y, bubbles: true }));
const press = async (el: { element: Element }, x: number, y: number) => {
  el.element.dispatchEvent(new PointerEvent("pointerdown", { clientX: x, clientY: y, button: 0, bubbles: true, cancelable: true }));
  await Promise.resolve();
};
afterEach(() => {
  document.body.innerHTML = "";
});

test("dragging a box moves it, on a 8 px grid, and the click that ends the drag selects nothing", async () => {
  const w = editable(graphOf(STORY_YAML));
  const base = w.get("[data-box][data-node=base]"); // at 160, 0
  await press(base, 200, 40);
  pointer("pointermove", 263, 101);
  await w.vm.$nextTick();
  expect(base.attributes("style")).toContain("left: 248px"); // follows the pointer while dragging
  pointer("pointerup", 263, 101);
  await w.vm.$nextTick();
  expect(w.emitted("move")).toEqual([["base", 224, 64]]);
  await base.trigger("click");
  expect(w.emitted("select")).toBeUndefined();
});

test("a press without a drag is still a click", async () => {
  const w = editable(graphOf(STORY_YAML));
  const base = w.get("[data-box][data-node=base]");
  await press(base, 200, 40);
  pointer("pointerup", 201, 40);
  await base.trigger("click");
  expect(w.emitted("move")).toBeUndefined();
  expect(w.emitted("select")).toEqual([["base"]]);
});

test("a wire dragged from a box's right port to another box connects them", async () => {
  const w = editable(graphOf(STORY_YAML));
  await press(w.get("[data-port=out][data-node=intake]"), 144, 52);
  pointer("pointermove", 190, 50);
  await w.vm.$nextTick();
  expect(w.find("[data-wire-draft]").exists()).toBe(true);
  pointer("pointerup", 190, 50); // over base's left edge
  await w.vm.$nextTick();
  expect(w.emitted("connect")).toEqual([["intake", "base"]]);
  expect(w.find("[data-wire-draft]").exists()).toBe(false);
});

test("a wire let go on its own box connects nothing", async () => {
  const w = editable(graphOf(STORY_YAML));
  await press(w.get("[data-port=out][data-node=intake]"), 144, 52);
  pointer("pointerup", 26, 52); // intake's own left port
  expect(w.emitted("connect")).toBeUndefined();
});

test("a wire let go over nothing connects nothing", async () => {
  const w = editable(graphOf(STORY_YAML));
  await press(w.get("[data-port=out][data-node=intake]"), 144, 52);
  pointer("pointerup", 150, 400);
  expect(w.emitted("connect")).toBeUndefined();
});

test("a click on a wire selects it, and the selected wire is marked", async () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph);
  await w.get("[data-edge-hit][data-from=intake][data-to=base]").trigger("click");
  expect(w.emitted("selectEdge")).toEqual([[graph.edges.find((e) => e.from === "intake" && e.to === "base")]]);
  await w.setProps({ selectedEdge: graph.edges.find((e) => e.from === "intake" && e.to === "base") });
  expect(w.get("[data-edge][data-from=intake][data-to=base]").attributes("data-selected")).toBe("true");
});

test("a box with problems is outlined and says them on hover", () => {
  const w = editable(graphOf(STORY_YAML), { problems: { base: ["check needs run"] } });
  const base = w.get("[data-box][data-node=base]");
  expect(base.attributes("data-problem")).toBe("true");
  expect(base.attributes("title")).toContain("check needs run");
  expect(w.get("[data-box][data-node=gate]").attributes("data-problem")).toBeUndefined();
});

test("a node type or a bundle dropped from the palette is placed where it lands", async () => {
  const w = editable(graphOf(STORY_YAML));
  const data = (kind: string, value: string) => ({ getData: (k: string) => (k === kind ? value : ""), types: [kind] });
  await w.get("[data-canvas]").trigger("drop", { clientX: 400, clientY: 300, dataTransfer: data("application/x-airlie-node", "check") });
  await w.get("[data-canvas]").trigger("drop", { clientX: 100, clientY: 100, dataTransfer: data("application/x-airlie-bundle", "layer") });
  expect(w.emitted("place")).toEqual([
    [{ type: "check" }, 320, 248],
    [{ bundle: "layer" }, 16, 48],
  ]);
});

test("read-only, a canvas has no ports and moves nothing", async () => {
  const w = mountCanvas(graphOf(STORY_YAML));
  expect(w.find("[data-port]").exists()).toBe(false);
  await press(w.get("[data-box][data-node=base]"), 200, 40);
  pointer("pointermove", 300, 100);
  pointer("pointerup", 300, 100);
  expect(w.emitted("move")).toBeUndefined();
});
