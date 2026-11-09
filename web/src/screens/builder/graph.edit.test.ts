// The graph's editing functions (graph.ts) on schema v2: a line is a `to` entry, what it carries is what its source
// writes and its target reads, and every edit keeps the node fields and the lines in step.
import { addNode, connect, emptyGraph, fromWorkflow, moveNode, readable, removeEdge, removeNode, renameNode, renameOutput, toWorkflow, updateNode } from "./graph";
import type { Graph } from "./graph";
import { parseWorkflow } from "./yaml";

/** a writes notes and verdict and goes on to b (which reads notes); c reads nothing yet; d writes notes too. */
const SMALL = `workflows:
  w:
    inputs: [card]
    nodes:
      a: {type: check, run: x.a, inputs: [card], outputs: {verdict: text, notes: markdown}, to: [b]}
      b: {type: ask, role: coder, prompt: "Fix {notes}; {?notes}again {notes.why}{/notes}", inputs: [notes]}
      c: {type: check, run: x.c}
      d: {type: check, run: x.d, outputs: {notes: markdown}}
    layout:
      a: {x: 0, y: 0}
      b: {x: 160, y: 0}
      c: {x: 320, y: 0}
      d: {x: 0, y: 90}
`;
const graphOf = (text: string): Graph => {
  const { name, spec, layout } = parseWorkflow(text);
  return fromWorkflow(name, spec, layout);
};
const small = () => graphOf(SMALL);
const specOf = (g: Graph, id: string) => g.nodes.find((n) => n.id === id)!.spec;
const lineOf = (g: Graph, from: string, to: string) => g.edges.filter((e) => e.from === from && e.to === to);

test("connect draws the line and adds each artifact it carries to the target's inputs", () => {
  const g = connect(small(), "a", "c", { artifacts: ["verdict"] });
  expect(lineOf(g, "a", "c")).toEqual([{ from: "a", to: "c", artifacts: ["verdict"] }]);
  expect(specOf(g, "c").inputs).toEqual(["verdict"]);
  expect(toWorkflow(g).spec.nodes.a.to).toEqual(["b", "c"]);
});

test("connect adds an input once: a second line between the same nodes replaces the first", () => {
  let g = connect(small(), "a", "c", { artifacts: ["verdict"] });
  g = connect(g, "a", "c", { artifacts: ["verdict", "notes"], when: "verdict == yes" });
  expect(specOf(g, "c").inputs).toEqual(["verdict", "notes"]);
  expect(lineOf(g, "a", "c")).toEqual([{ from: "a", to: "c", artifacts: ["verdict", "notes"], when: "verdict == yes" }]);
  // b reads notes already: no second input, no second line
  g = connect(g, "a", "b", { artifacts: ["notes"] });
  expect(specOf(g, "b").inputs).toEqual(["notes"]);
  expect(lineOf(g, "a", "b")).toHaveLength(1);
  expect(g.edges.map((e) => `${e.from}>${e.to}`)).toEqual(["a>b", "a>c"]); // the replaced line keeps its place
});

test("connect with no artifacts draws an ordering line; a loop line keeps its bound", () => {
  let g = connect(small(), "c", "d", {});
  expect(lineOf(g, "c", "d")).toEqual([{ from: "c", to: "d", artifacts: [] }]);
  expect(specOf(g, "d")).not.toHaveProperty("inputs");
  g = connect(g, "b", "a", { loop: { max: 3, carry: "notes" } });
  expect(lineOf(g, "b", "a")).toEqual([{ from: "b", to: "a", artifacts: [], loop: { max: 3, carry: "notes" } }]);
  expect(toWorkflow(g).spec.nodes.b.to).toEqual([{ node: "a", loop: { max: 3, carry: "notes" } }]);
});

test("connect refuses an artifact its source does not write, a loop without its max, a box that is not there", () => {
  expect(() => connect(small(), "c", "b", { artifacts: ["notes"] })).toThrow(/c writes no notes/);
  expect(() => connect(small(), "b", "a", { loop: { max: "" } })).toThrow(/max/);
  expect(() => connect(small(), "b", "a", { loop: { max: "  " } })).toThrow(/max/);
  expect(() => connect(small(), "a", "zz", {})).toThrow(/zz/);
});

test("removeEdge drops the line and the inputs it alone carried", () => {
  let g = connect(small(), "a", "c", { artifacts: ["verdict", "notes"] });
  g = updateNode(g, "c", { ...specOf(g, "c"), inputs: ["card", "verdict", "notes"] });
  g = connect(g, "d", "c", { artifacts: ["notes"] });
  g = removeEdge(g, lineOf(g, "a", "c")[0]);
  expect(lineOf(g, "a", "c")).toEqual([]);
  // verdict went with the line; notes still comes from d; card is the workflow's input, no line's
  expect(specOf(g, "c").inputs).toEqual(["card", "notes"]);
  g = removeEdge(g, lineOf(g, "d", "c")[0]);
  expect(specOf(g, "c").inputs).toEqual(["card"]);
});

test("removeEdge of an ordering line touches no input", () => {
  const g = connect(small(), "c", "b", {});
  expect(specOf(removeEdge(g, lineOf(g, "c", "b")[0]), "b").inputs).toEqual(["notes"]);
});

test("renameOutput renames the output, the lines that carry it and its readers' inputs and placeholders", () => {
  const before = connect(small(), "a", "c", { artifacts: ["notes"] });
  const g = renameOutput(before, "a", "notes", "remarks");
  if ("refused" in g) throw new Error("refused");
  expect(specOf(g, "a").outputs).toEqual({ verdict: "text", remarks: "markdown" });
  expect(Object.keys(specOf(g, "a").outputs as object)).toEqual(["verdict", "remarks"]); // in place
  expect(lineOf(g, "a", "b")[0].artifacts).toEqual(["remarks"]);
  expect(lineOf(g, "a", "c")[0].artifacts).toEqual(["remarks"]);
  expect(specOf(g, "b").inputs).toEqual(["remarks"]);
  expect(specOf(g, "c").inputs).toEqual(["remarks"]);
  expect(specOf(g, "b").prompt).toBe("Fix {remarks}; {?remarks}again {remarks.why}{/remarks}");
  // d's own notes is another writer's: untouched
  expect(specOf(g, "d").outputs).toEqual({ notes: "markdown" });
});

test("renameOutput refuses when a reader's prompt file names it, listing the readers", () => {
  const g = updateNode(small(), "b", { ...specOf(small(), "b"), prompt: "prompt:fix-brief" });
  expect(renameOutput(g, "a", "notes", "remarks")).toEqual({ refused: ["b"] });
  // a prompt file known not to name it is no reason to refuse
  const ok = renameOutput(g, "a", "notes", "remarks", { "fix-brief": ["card"] });
  expect("refused" in ok).toBe(false);
  expect(renameOutput(g, "a", "notes", "remarks", { "fix-brief": ["notes"] })).toEqual({ refused: ["b"] });
});

test("renameOutput follows a reader that names it as its session or a bundle's each, not in its inputs", () => {
  const text = `workflows:
  w:
    nodes:
      p: {type: check, run: x.p, outputs: {plan: session, items: list}, to: [c, b]}
      c: {type: write, role: architect, prompt: "go on", session: plan}
      b: {type: bundle, workflow: layer, each: items}
`;
  let g = renameOutput(graphOf(text), "p", "plan", "talk");
  if ("refused" in g) throw new Error("refused");
  expect(specOf(g, "c")).toEqual({ role: "architect", prompt: "go on", session: "talk" });
  g = renameOutput(g, "p", "items", "layers");
  if ("refused" in g) throw new Error("refused");
  expect(specOf(g, "b")).toEqual({ workflow: "layer", each: "layers" });
});

test("renameOutput refuses a name taken or an output that is not there", () => {
  expect(() => renameOutput(small(), "a", "notes", "verdict")).toThrow(/already/);
  expect(() => renameOutput(small(), "a", "nope", "x")).toThrow(/nope/);
});

test("renameOutput of a bundle workflow's declared output renames it there too", () => {
  const g = renameOutput({ ...small(), outputs: ["notes"] }, "a", "notes", "remarks");
  if ("refused" in g) throw new Error("refused");
  expect(g.outputs).toEqual(["remarks"]);
});

test("addNode gives each type its default fields, names it <type>-N and puts it last, with no line", () => {
  let g = addNode(small(), "check", 480, 0);
  expect(g.nodes.at(-1)).toMatchObject({ id: "check-1", type: "check", spec: { run: "", inputs: [], outputs: {} }, x: 480, y: 0, placed: true });
  expect(g.edges).toEqual(small().edges);
  const blank = (type: Parameters<typeof addNode>[1]) => specOf(addNode(emptyGraph("w"), type, 0, 0), `${type}-1`);
  expect(blank("ask")).toEqual({ role: "", prompt: "", inputs: [], outputs: {} });
  expect(blank("conversation")).toEqual({ role: "", prompt: "", inputs: [], outputs: {} });
  expect(blank("write")).toEqual({ role: "", prompt: "", inputs: [], outputs: {} });
  expect(blank("meeting")).toEqual({ role: [], prompt: "", inputs: [], outputs: {} });
  expect(blank("team")).toEqual({ role: { supervisor: "", worker: "" }, prompt: "", worker: "", inputs: [], outputs: {} });
  expect(blank("gate")).toEqual({ run: "", inputs: [], outputs: {} });
  expect(blank("bundle")).toEqual({ workflow: "", inputs: [], outputs: {} });
  // the caller hands in what the catalog says (a meeting's seats and inputs, a bundle's workflow)
  g = addNode(emptyGraph("w"), "meeting", 0, 0, { role: ["product", "architect", "tester"], inputs: ["story", "layers"] });
  expect(specOf(g, "meeting-1")).toEqual({ role: ["product", "architect", "tester"], prompt: "", inputs: ["story", "layers"], outputs: {} });
  g = addNode(addNode(addNode(g, "check", 0, 0), "check", 0, 0), "check", 0, 0);
  expect(addNode(removeNode(g, "check-2"), "check", 0, 0).nodes.map((n) => n.id)).toEqual(["meeting-1", "check-1", "check-3", "check-2"]);
});

test("removeNode drops the box and every line into or out of it", () => {
  let g = connect(small(), "c", "a", {});
  g = removeNode(g, "a");
  expect(g.nodes.map((n) => n.id)).toEqual(["b", "c", "d"]);
  expect(g.edges).toEqual([]);
});

test("renameNode renames a box, its lines and a feedback.<id> input", () => {
  let g = connect(small(), "b", "a", { loop: { max: 2, carry: "notes" } });
  g = updateNode(g, "c", { run: "x.c", inputs: ["feedback.a"] });
  g = renameNode(g, "a", "first");
  expect(g.nodes.map((n) => n.id)).toEqual(["first", "b", "c", "d"]);
  expect(g.edges.map((e) => `${e.from}>${e.to}`)).toEqual(["first>b", "b>first"]);
  expect(lineOf(g, "first", "b")[0].artifacts).toEqual(["notes"]);
  expect(specOf(g, "c").inputs).toEqual(["feedback.first"]);
  expect(() => renameNode(g, "b", "c")).toThrow(/already/);
});

test("moveNode moves and places a box and nothing else", () => {
  const before = small();
  const g = moveNode(before, "c", 80, 90);
  expect(g.nodes.find((n) => n.id === "c")).toMatchObject({ x: 80, y: 90, placed: true });
  expect(g.edges).toEqual(before.edges);
  expect(g.nodes.map((n) => n.spec)).toEqual(before.nodes.map((n) => n.spec));
  expect(toWorkflow(g).spec).toEqual(toWorkflow(before).spec);
});

test("updateNode replaces a box's fields and the lines' artifacts follow them", () => {
  const g = updateNode(small(), "b", { role: "coder", prompt: "p", inputs: ["card"] });
  expect(specOf(g, "b")).toEqual({ role: "coder", prompt: "p", inputs: ["card"] });
  expect(lineOf(g, "a", "b")).toEqual([{ from: "a", to: "b", artifacts: [] }]);
});

test("readable: the workflow's inputs, the other boxes' outputs, and the feedback a loop line carries back to it", () => {
  expect(readable(small(), "b")).toEqual(["card", "verdict", "notes"]);
  expect(readable(small(), "d")).toEqual(["card", "verdict", "notes"]);
  const g = connect(small(), "b", "a", { loop: { max: 2, carry: "notes" } });
  expect(readable(g, "a")).toEqual(["card", "notes", "feedback.a"]);
});

test("toWorkflow: the inputs, outputs and nodes in the graph's order; a line is a bare name unless it has a when or a loop", () => {
  let g = connect(small(), "a", "c", { when: "verdict == yes" });
  g = moveNode(g, "a", 480, 0);
  const { spec, layout } = toWorkflow(g);
  expect(spec.inputs).toEqual(["card"]);
  expect(spec.outputs).toEqual([]);
  expect(Object.keys(spec.nodes)).toEqual(["a", "b", "c", "d"]);
  expect(spec.nodes.a).toMatchObject({ type: "check", run: "x.a", to: ["b", { node: "c", when: "verdict == yes" }] });
  expect(spec.nodes.c).not.toHaveProperty("to");
  expect(layout.a).toEqual({ x: 480, y: 0 });
  expect(toWorkflow(emptyGraph("new")).spec).toEqual({ inputs: ["card", "repo"], outputs: [], nodes: {} });
});
