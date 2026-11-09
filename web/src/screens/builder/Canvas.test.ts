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
  expect(w.get("[data-edge=line][data-from=intake][data-to=base]").attributes("d")).toMatch(/^M 144 /); // intake's right edge
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
  expect(mountCanvas(graphOf(text)).get("[data-edge-label]").text()).toBe("if v == yes · order");
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

test("a wire dragged from a box's out-port to another box asks to connect them", async () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph);
  const to = graph.nodes.find((n) => n.id === "request")!;
  await press(w.get("[data-port=out][data-node=base]"), 144, 52);
  pointer("pointermove", to.x + 60, to.y + 40);
  await w.vm.$nextTick();
  expect(w.find("[data-wire-draft]").exists()).toBe(true);
  pointer("pointerup", to.x + 60, to.y + 40); // over request, which has no line from base
  await w.vm.$nextTick();
  expect(w.emitted("connect")).toEqual([["base", "request", { artifacts: ["base"] }]]);
  expect(w.find("[data-wire-draft]").exists()).toBe(false);
});

test("a wire let go on its own box connects nothing", async () => {
  const w = editable(graphOf(STORY_YAML));
  await press(w.get("[data-port=edge][data-node=intake]"), 144, 52);
  pointer("pointerup", 26, 52); // intake's own left port
  expect(w.emitted("connect")).toBeUndefined();
});

test("a wire let go over nothing connects nothing", async () => {
  const w = editable(graphOf(STORY_YAML));
  await press(w.get("[data-port=edge][data-node=intake]"), 144, 52);
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

test("the selected wire shows a card whose Edit opens it; no card on a readonly canvas or an unselected wire", async () => {
  const graph = graphOf(STORY_YAML);
  const edge = graph.edges.find((e) => e.from === "intake" && e.to === "base")!;
  const w = editable(graph);
  expect(w.find("[data-edge-card]").exists()).toBe(false);
  await w.setProps({ selectedEdge: edge });
  expect(w.get("[data-edge-card]").text()).toContain("intake → base");
  await w.get("[data-edge-card] [data-edge-edit]").trigger("click");
  expect(w.emitted("openEdge")).toEqual([[edge]]);
  const ro = mountCanvas(graph);
  await ro.setProps({ selectedEdge: edge });
  expect(ro.find("[data-edge-card]").exists()).toBe(false);
});

test("a wire with a problem has its label marked", () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph, { edgeProblems: { "intake>base": ["line to base: condition names 'x', which nothing produces"] } });
  const marked = w.findAll("[data-edge-label][data-edge-problem]");
  expect(marked).toHaveLength(1);
  expect(marked[0].element.parentElement!.querySelector("[data-edge][data-from=intake][data-to=base]")).not.toBeNull();
});

test("an out-port's label is hidden until the port is hovered and then capped, so it never sits over the box's id or sub text", () => {
  const w = editable(graphOf(STORY_YAML));
  const labels = w.findAll("[data-port-label]");
  expect(labels.length).toBeGreaterThan(0);
  for (const l of labels) {
    expect(l.classes()).toContain("hidden");
    expect(l.classes()).toContain("group-hover:block");
    expect(l.classes()).toContain("truncate");
    expect(l.classes()).toContain("max-w-[160px]");
  }
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

// ---- ports, labelled lines, bundle boxes, the box card ----

const pos = (graph: Graph, id: string) => {
  const n = graph.nodes.find((x) => x.id === id)!;
  return { x: n.x + 24, y: n.y + 24 };
};
const outs = (w: ReturnType<typeof editable>, id: string) => w.findAll(`[data-port=out][data-node=${id}]`).map((p) => p.attributes("data-artifact"));

test("a box has an out-port per output, one edge port and one in-port", () => {
  const w = editable(graphOf(STORY_YAML));
  expect(outs(w, "gate")).toEqual(["gate"]);
  expect(outs(w, "survey")).toEqual(["survey"]);
  expect(outs(w, "layers")).toEqual(["layers"]);
  expect(outs(w, "intake")).toEqual([]);
  expect(w.findAll("[data-port=edge][data-node=gate]")).toHaveLength(1);
  expect(w.findAll("[data-port=in][data-node=gate]")).toHaveLength(1);
});

test("a box with several outputs stacks one port each down its right edge, inside the box", () => {
  const text = "workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a, outputs: {one: text, two: text, three: text}}\n      b: {type: check, run: x.b}\n";
  const w = editable(graphOf(text));
  expect(outs(w, "a")).toEqual(["one", "two", "three"]);
  const ports = w.findAll("[data-node=a]").filter((p) => ["out", "edge"].includes(p.attributes("data-port") ?? ""));
  expect(ports).toHaveLength(4);
  const tops = ports.map((p) => Number(/top: ([\d.]+)px/.exec(p.attributes("style") ?? "")![1]));
  expect(new Set(tops).size).toBe(4);
  const box = w.get("[data-box][data-node=a]");
  const height = Number(/height: (\d+)px/.exec(box.attributes("style") ?? "")![1]);
  for (const t of tops) {
    expect(t).toBeGreaterThan(24);
    expect(t).toBeLessThan(24 + height);
  }
});

test("a drag from the survey port onto a box that does not read it asks for a line carrying survey", async () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph);
  const to = pos(graph, "refine");
  await press(w.get("[data-port=out][data-node=survey][data-artifact=survey]"), 0, 0);
  pointer("pointerup", to.x + 60, to.y + 20);
  await w.vm.$nextTick();
  expect(w.emitted("connect")).toEqual([["survey", "refine", { artifacts: ["survey"] }]]);
  expect(w.emitted("selectEdge")).toBeUndefined();
});

test("a drag from the edge port asks for an ordering line", async () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph);
  const to = pos(graph, "refine");
  await press(w.get("[data-port=edge][data-node=survey]"), 0, 0);
  pointer("pointerup", to.x + 60, to.y + 20);
  expect(w.emitted("connect")).toEqual([["survey", "refine", {}]]);
});

test("a drop on a box that already reads the artifact selects the existing line and asks for nothing else", async () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph);
  const to = pos(graph, "survey_note"); // reads survey, over a line from survey
  await press(w.get("[data-port=out][data-node=survey][data-artifact=survey]"), 0, 0);
  pointer("pointerup", to.x + 60, to.y + 20);
  expect(w.emitted("selectEdge")).toEqual([[graph.edges.find((e) => e.from === "survey" && e.to === "survey_note")]]);
  expect(w.emitted("connect")).toBeUndefined();
});

test("every line is labelled with what it carries, or order", () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph);
  const labels = w.findAll("[data-edge-label]").map((l) => l.text());
  expect(labels).toHaveLength(graph.edges.length);
  expect(labels.some((t) => t.includes("survey") && !t.startsWith("if"))).toBe(true);
  expect(labels).toContain("order");
  const order = editable(graphOf("workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a}\n      b: {type: check, run: x.b}\n      a2: {type: check, run: x.c}\n"));
  expect(order.findAll("[data-edge-label]")).toHaveLength(0);
});

test("a condition shows as an if prefix and a loop as dashed with its bound, together", () => {
  const text =
    "workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a, outputs: {v: text}, to: [{node: b, when: v == no}]}\n      b: {type: check, run: x.b, inputs: [v], to: [{node: a, when: v == no, loop: {max: 2}}]}\n";
  const w = editable(graphOf(text));
  expect(w.get("[data-edge=loop][data-from=b][data-to=a]").attributes("stroke-dasharray")).toBeTruthy();
  const labels = w.findAll("[data-edge-label]").map((l) => l.text());
  expect(labels).toContain("if v == no · v");
  expect(labels).toContain("if v == no · order · loop ×2");
});

test("the layer's loop reads loop ×<max>", () => {
  const w = mountCanvas(graphOf(LAYER_YAML));
  expect(w.findAll("[data-edge-label]").some((l) => /loop ×\S+/.test(l.text()))).toBe(true);
});

test("line geometry and labels stay inside the canvas", () => {
  for (const text of [STORY_YAML, LAYER_YAML]) {
    const w = editable(graphOf(text));
    const canvas = w.get("[data-canvas]").attributes("style") ?? "";
    const width = Number(/width: ([\d.]+)px/.exec(canvas)![1]);
    const height = Number(/height: ([\d.]+)px/.exec(canvas)![1]);
    for (const path of w.findAll("[data-edge]")) {
      const nums = (path.attributes("d") ?? "").match(/-?\d+(\.\d+)?/g)!.map(Number);
      for (let i = 0; i < nums.length; i += 2) {
        expect(nums[i]).toBeGreaterThanOrEqual(0);
        expect(nums[i]).toBeLessThanOrEqual(width);
      }
    }
    for (const l of w.findAll("[data-edge-label]")) {
      expect(Number(l.attributes("y"))).toBeGreaterThan(0);
      expect(Number(l.attributes("y"))).toBeLessThan(height);
    }
  }
});

test("the bundle box shows built.{item} and satisfied.{item} out-ports", () => {
  const w = editable(graphOf(STORY_YAML));
  expect(outs(w, "build_layers")).toEqual(["built", "satisfied"]);
  expect(w.findAll("[data-port=out][data-node=build_layers]").map((p) => p.text())).toEqual(["built.{item}", "satisfied.{item}"]);
  expect(w.get("[data-box][data-node=build_layers]").attributes("data-shape")).toBe("bundle");
});

test("a bundle box that declares no outputs shows the workflow's declared ones", () => {
  const graph = graphOf("workflows:\n  w:\n    nodes:\n      b: {type: bundle, workflow: layer, each: layers}\n");
  const layer = graphOf(LAYER_YAML);
  const w = editable({ ...graph, bundles: { layer } });
  expect(w.findAll("[data-port=out][data-node=b]").map((p) => p.text())).toEqual(["drafted.{item}", "built.{item}", "satisfied.{item}"]);
});

test("the selected box shows its card; Edit and a double-click open the box", async () => {
  const w = editable(graphOf(STORY_YAML), { selected: "gate" });
  const card = w.get("[data-box-card]");
  expect(card.text()).toContain("gate");
  expect(card.text()).toContain("check");
  await w.get("[data-box-edit]").trigger("click");
  expect(w.emitted("open")).toEqual([["gate"]]);
  await w.get("[data-box][data-node=base]").trigger("dblclick");
  expect(w.emitted("open")).toEqual([["gate"]]);
  expect(w.emitted("enter")).toEqual([["base"]]); // a double-click enters: a bundle box opens its workflow, any other box its fields
  await w.setProps({ selected: null });
  expect(w.find("[data-box-card]").exists()).toBe(false);
});

test("a double-click on a line opens it", async () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph);
  await w.get("[data-edge-hit][data-from=intake][data-to=base]").trigger("dblclick");
  expect(w.emitted("openEdge")).toEqual([[graph.edges.find((e) => e.from === "intake" && e.to === "base")]]);
});

// ---- fix round 1 ----

test("a drag from the layer bundle's built port to a downstream box connects with built", async () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph);
  const to = pos(graph, "story_card");
  await press(w.get("[data-port=out][data-node=build_layers][data-artifact=built]"), 0, 0);
  pointer("pointerup", to.x + 60, to.y + 20);
  expect(w.emitted("connect")).toEqual([["build_layers", "story_card", { artifacts: ["built"] }]]);
});

test("a bundle box that declares no outputs offers the workflow's, and a drag from one connects", async () => {
  const graph = graphOf("workflows:\n  w:\n    nodes:\n      b: {type: bundle, workflow: layer, each: layers}\n      c: {type: check, run: x.c}\n");
  const layer = graphOf(LAYER_YAML);
  const w = editable({ ...graph, bundles: { layer } });
  const to = pos(graph, "c");
  await press(w.get("[data-port=out][data-node=b][data-artifact=built]"), 0, 0);
  pointer("pointerup", to.x + 60, to.y + 20);
  expect(w.emitted("connect")).toEqual([["b", "c", { artifacts: ["built"] }]]);
});

test("read-only, a click shows no box card", async () => {
  const w = mountCanvas(graphOf(STORY_YAML), "gate");
  expect(w.find("[data-box-card]").exists()).toBe(false);
});

test("the box card stays inside the canvas, even for the lowest box", () => {
  const graph = graphOf(STORY_YAML);
  const lowest = [...graph.nodes].sort((a, b) => b.y - a.y)[0];
  const w = editable(graph, { selected: lowest.id });
  const canvas = w.get("[data-canvas]").attributes("style") ?? "";
  const height = Number(/height: ([\d.]+)px/.exec(canvas)![1]);
  const width = Number(/width: ([\d.]+)px/.exec(canvas)![1]);
  const style = w.get("[data-box-card]").attributes("style") ?? "";
  expect(Number(/top: ([\d.]+)px/.exec(style)![1])).toBeLessThan(height);
  expect(Number(/left: ([\d.]+)px/.exec(style)![1])).toBeLessThan(width);
});

test("a line whose first artifact is not one of the source's ports leaves from the edge port", () => {
  const text = "workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a, outputs: {one: text, two: text}, to: [b]}\n      b: {type: check, run: x.b, inputs: [one]}\n";
  const graph = graphOf(text);
  const stray = { ...graph, edges: graph.edges.map((e) => ({ ...e, artifacts: ["ghost"] })) };
  const w = editable(stray);
  const edgeTop = Number(/top: ([\d.]+)px/.exec(w.get("[data-port=edge][data-node=a]").attributes("style") ?? "")![1]);
  expect(w.get("[data-edge=line][data-from=a]").attributes("d")).toMatch(new RegExp(`^M 144 ${edgeTop} `));
});

test("the ports of a box do not overlap", () => {
  const text = "workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a, outputs: {one: text, two: text, three: text}}\n";
  const w = editable(graphOf(text));
  const tops = w
    .findAll("[data-node=a]")
    .filter((p) => ["out", "edge"].includes(p.attributes("data-port") ?? ""))
    .map((p) => Number(/top: ([\d.]+)px/.exec(p.attributes("style") ?? "")![1]));
  for (let i = 1; i < tops.length; i++) expect(tops[i]).toBeGreaterThanOrEqual(tops[i - 1] + 12); // a port is 12 px tall
});

test("labels read exactly: the artifact, the condition and order, the loop", () => {
  const w = editable(graphOf(STORY_YAML));
  const label = (from: string, to: string) => w.get(`[data-edge=line][data-from=${from}][data-to=${to}]`).element.parentElement!.querySelector("[data-edge-label]")!.textContent;
  expect(label("survey", "survey_note")).toBe("survey");
  expect(label("gate", "request")).toBe("if config.story.survey · order");
  expect(label("intake", "base")).toBe("order");
  const layer = editable(graphOf(LAYER_YAML));
  const loop = layer.get("[data-edge=loop]");
  expect(loop.element.parentElement!.querySelector("[data-edge-label]")!.textContent).toMatch(/^(if .+ · )?.+ · loop ×\S+$/);
});

test("a cancelled pointer clears the wire being drawn: no preview line, no connect", async () => {
  const graph = graphOf(STORY_YAML);
  const w = editable(graph);
  const to = pos(graph, "refine");
  await press(w.get("[data-port=out][data-node=survey]"), 0, 0);
  pointer("pointermove", to.x + 60, to.y + 20);
  await w.vm.$nextTick();
  expect(w.find("[data-wire-draft]").exists()).toBe(true);
  window.dispatchEvent(new PointerEvent("pointercancel"));
  await w.vm.$nextTick();
  expect(w.find("[data-wire-draft]").exists()).toBe(false);
  pointer("pointerup", to.x + 60, to.y + 20); // the listeners are gone
  expect(w.emitted("connect")).toBeUndefined();
});
