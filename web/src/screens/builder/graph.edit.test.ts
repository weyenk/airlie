// The graph's editing functions (graph.ts): each produces the spec keys its move means, and the wires are what
// the composer would walk from those keys.
import { addNode, canRemove, connect, emptyGraph, fromWorkflow, isChild, moveNode, removeEdge, removeNode, renameNode, toWorkflow, updateNode } from "./graph";
import type { Graph } from "./graph";
import { parseWorkflow } from "./yaml";

/** Three checks a > b > c in a row, 160 px apart, falling through in canvas order. */
const ROW3 = `workflows:
  w:
    start: a
    nodes:
      a: {type: check, run: x.a, outputs: {verdict: text, notes: markdown}}
      b: {type: check, run: x.b}
      c: {type: check, run: x.c}
    layout:
      a: {x: 0, y: 0}
      b: {x: 160, y: 0}
      c: {x: 320, y: 0}
`;
const graphOf = (text: string): Graph => {
  const { name, spec, layout } = parseWorkflow(text);
  return fromWorkflow(name, spec, layout);
};
const row3 = () => graphOf(ROW3);
const specOf = (g: Graph, id: string) => g.nodes.find((n) => n.id === id)!.spec;
const wires = (g: Graph) => g.edges.map((e) => `${e.kind}:${e.from}>${e.to}${e.label ? `[${e.label}]` : ""}`);

test("addNode names a box <type>-N with the smallest free N, gives its type's default fields and places it", () => {
  let g = addNode(row3(), "check", 480, 0);
  expect(g.nodes.find((n) => n.id === "check-1")).toMatchObject({ type: "check", spec: { run: "" }, x: 480, y: 0, placed: true });
  expect(wires(g)).toContain("sequence:c>check-1"); // a box dropped after the last one is what it falls through to
  g = addNode(g, "ask", 640, 0);
  expect(specOf(g, "ask-1")).toEqual({ role: "", prompt: "", outputs: {} });
  g = addNode(addNode(g, "check", 800, 0), "check", 960, 0);
  g = removeNode(g, "check-2");
  expect(addNode(g, "check", 1120, 0).nodes.map((n) => n.id)).toContain("check-2");
});

test("addNode can bring fields of its own (an each box from a bundle)", () => {
  expect(specOf(addNode(row3(), "each", 480, 0, { workflow: "layer" }), "each-1")).toEqual({ over: "", workflow: "layer" });
});

test("addNode on an empty canvas makes the box the start", () => {
  const g = addNode(emptyGraph("new"), "gate", 0, 0);
  expect(g.start).toBe("gate-1");
  expect(toWorkflow(g).spec.start).toBe("gate-1");
});

test("addNode dropped between two boxes changes no wire it lands on: it goes last in the file", () => {
  const g = addNode(row3(), "check", 80, 90);
  expect(g.nodes.map((n) => n.id)).toEqual(["a", "b", "c", "check-1"]);
  expect(wires(g)).toEqual(["sequence:a>b", "sequence:b>c", "sequence:c>check-1"]);
  expect(specOf(g, "a")).not.toHaveProperty("next");
});

test("connect sequence to the box that follows on the canvas sets no next; to another box it does", () => {
  expect(specOf(connect(row3(), "a", "b", "sequence"), "a")).not.toHaveProperty("next");
  const g = connect(row3(), "a", "c", "sequence");
  expect(specOf(g, "a").next).toBe("c");
  expect(wires(g)).toContain("sequence:a>c");
  expect(wires(g)).not.toContain("sequence:a>b");
  // back to the follower drops the next again
  expect(specOf(connect(g, "a", "b", "sequence"), "a")).not.toHaveProperty("next");
});

test("connect parallel creates a parallel box between source and target holding the target, then reuses it", () => {
  let g = connect(row3(), "a", "c", "parallel");
  const p = g.nodes.find((n) => n.type === "parallel")!;
  expect(p.id).toBe("parallel-1");
  expect(p.spec.nodes).toEqual(["c"]);
  expect(wires(g)).toEqual(expect.arrayContaining(["sequence:a>parallel-1", "parallel:parallel-1>c", "parallel:c>b"]));
  expect(wires(g)).not.toContain("sequence:a>b");
  g = connect(g, "a", "b", "parallel");
  expect(specOf(g, "parallel-1").nodes).toEqual(["c", "b"]);
  expect(g.nodes.filter((n) => n.type === "parallel")).toHaveLength(1);
});

test("a parallel child drops its next, loop and branch: a child may carry none", () => {
  let g = connect(row3(), "c", "a", "loop", { loop: { max: 2 } });
  g = updateNode(g, "c", { ...specOf(g, "c"), next: "a", on: "verdict", branch: { yes: "b" } });
  g = connect(g, "a", "c", "parallel");
  expect(specOf(g, "c")).toEqual({ run: "x.c" });
  expect(isChild(g, "c")).toBe(true);
  expect(isChild(g, "a")).toBe(false);
});

test("connect parallel from a parallel box adds the target to its nodes", () => {
  const g = connect(connect(row3(), "a", "c", "parallel"), "parallel-1", "b", "parallel");
  expect(specOf(g, "parallel-1").nodes).toEqual(["c", "b"]);
});

test("connect loop sets loop {to, max, when, carry} on the source", () => {
  const g = connect(row3(), "c", "a", "loop", { loop: { max: "story.layer_attempts", when: "x.again", carry: "notes" } });
  expect(specOf(g, "c").loop).toEqual({ to: "a", max: "story.layer_attempts", when: "x.again", carry: "notes" });
  expect(g.edges.filter((e) => e.kind === "loop")).toEqual([{ from: "c", to: "a", kind: "loop", loop: { max: "story.layer_attempts", when: "x.again", carry: "notes" } }]);
  expect(specOf(connect(row3(), "c", "a", "loop", { loop: { max: 3 } }), "c").loop).toEqual({ to: "a", max: 3 });
});

test("connect branch sets on and branch[value] on the source, adding to the values it has", () => {
  let g = connect(row3(), "a", "c", "branch", { branch: { on: "verdict", value: "yes" } });
  expect(specOf(g, "a")).toMatchObject({ on: "verdict", branch: { yes: "c" } });
  g = connect(g, "a", "b", "branch", { branch: { on: "verdict", value: "no" } });
  expect(specOf(g, "a").branch).toEqual({ yes: "c", no: "b" });
  expect(wires(g)).toEqual(expect.arrayContaining(["branch:a>c[yes]", "branch:a>b[no]"]));
});

test("connect each sets the each box's workflow and wires to it", () => {
  let g = addNode(row3(), "each", 480, 0);
  g = connect(g, "c", "each-1", "each", { each: { workflow: "layer" } });
  expect(specOf(g, "each-1")).toMatchObject({ workflow: "layer" });
  expect(wires(g)).toContain("sequence:c>each-1");
  expect(() => connect(row3(), "a", "b", "each", { each: { workflow: "layer" } })).toThrow(/each/);
});

test("a move missing its fields is refused", () => {
  expect(() => connect(row3(), "c", "a", "loop")).toThrow(/max/);
  expect(() => connect(row3(), "a", "b", "branch", { branch: { on: "", value: "yes" } })).toThrow(/on/);
});

test("removeEdge drops what made the wire: a next, a branch value, a loop, a parallel child", () => {
  let g = connect(row3(), "a", "c", "sequence");
  g = removeEdge(g, g.edges.find((e) => e.from === "a")!);
  expect(specOf(g, "a")).not.toHaveProperty("next");
  g = connect(row3(), "a", "c", "branch", { branch: { on: "verdict", value: "yes" } });
  g = removeEdge(g, g.edges.find((e) => e.kind === "branch")!);
  expect(specOf(g, "a")).not.toHaveProperty("branch");
  expect(specOf(g, "a")).not.toHaveProperty("on");
  g = connect(row3(), "c", "a", "loop", { loop: { max: 2 } });
  g = removeEdge(g, g.edges.find((e) => e.kind === "loop")!);
  expect(specOf(g, "c")).not.toHaveProperty("loop");
  g = connect(row3(), "a", "c", "parallel");
  g = removeEdge(g, g.edges.find((e) => e.from === "parallel-1" && e.to === "c")!);
  expect(specOf(g, "parallel-1").nodes).toEqual([]);
  // the canvas order is not a field: a fall-through wire stays
  const plain = row3();
  expect(canRemove(plain, plain.edges[0])).toBe(false);
  expect(removeEdge(plain, plain.edges[0]).edges).toEqual(plain.edges);
});

test("removeNode drops the box, its wires and every reference to it", () => {
  let g = connect(row3(), "c", "b", "loop", { loop: { max: 2 } });
  g = connect(g, "a", "b", "branch", { branch: { on: "verdict", value: "yes" } });
  g = removeNode(g, "b");
  expect(g.nodes.map((n) => n.id)).toEqual(["a", "c"]);
  expect(g.edges.some((e) => e.from === "b" || e.to === "b")).toBe(false);
  expect(specOf(g, "c")).not.toHaveProperty("loop");
  expect(specOf(g, "a")).not.toHaveProperty("branch");
  expect(removeNode(g, "a").start).toBe("c");
});

test("moveNode moves and places a box and nothing else: the wires and fields stay though it sits elsewhere", () => {
  const before = row3();
  const g = moveNode(before, "c", 80, 90); // c now sits between a and b
  expect(g.nodes.find((n) => n.id === "c")).toMatchObject({ x: 80, y: 90, placed: true });
  expect(wires(g)).toEqual(wires(before));
  expect(g.nodes.map((n) => n.spec)).toEqual(before.nodes.map((n) => n.spec));
  expect(g.nodes.map((n) => n.id)).toEqual(["a", "b", "c"]);
});

test("updateNode replaces a box's fields and the wires follow them", () => {
  const g = updateNode(row3(), "a", { run: "x.a", next: "c" });
  expect(specOf(g, "a")).toEqual({ run: "x.a", next: "c" });
  expect(wires(g)).toContain("sequence:a>c");
});

test("renameNode renames a box and every reference to it", () => {
  let g = connect(row3(), "c", "a", "loop", { loop: { max: 2, carry: "notes" } });
  g = updateNode(g, "b", { ...specOf(g, "b"), inputs: ["feedback.a"] });
  g = renameNode(g, "a", "first");
  expect(g.start).toBe("first");
  expect(specOf(g, "c").loop).toMatchObject({ to: "first" });
  expect(specOf(g, "b").inputs).toEqual(["feedback.first"]);
  expect(() => renameNode(g, "b", "c")).toThrow(/already/);
});

test("toWorkflow: the spec in the file's order with its layout; no start until a box exists", () => {
  const { spec, layout } = toWorkflow(moveNode(row3(), "a", 480, 0));
  expect(Object.keys(spec.nodes)).toEqual(["a", "b", "c"]);
  expect(spec.nodes.a).toMatchObject({ type: "check", run: "x.a" });
  expect(layout).toEqual({ a: { x: 480, y: 0 }, b: { x: 160, y: 0 }, c: { x: 320, y: 0 } });
  expect(toWorkflow(emptyGraph("new")).spec).not.toHaveProperty("start");
});

test("a branch without on draws no branch wires and falls through", () => {
  const g = graphOf("workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a, branch: {'*': c}}\n      b: {type: check, run: x.b}\n      c: {type: check, run: x.c}\n");
  expect(g.edges.filter((e) => e.kind === "branch")).toEqual([]);
  expect(wires(g)).toContain("sequence:a>b");
});
