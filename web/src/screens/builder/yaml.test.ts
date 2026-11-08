import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { connect, emptyGraph, fromWorkflow } from "./graph";
import { autoLayout } from "./layout";
import { parseWorkflow, toYaml } from "./yaml";

test("parseWorkflow reads the one workflow in a file: its name, spec and (absent) layout", () => {
  const { name, spec, layout } = parseWorkflow(STORY_YAML);
  expect(name).toBe("story");
  expect(spec).toMatchObject({ type: "story", start: "intake", loose: ["card", "repo"] });
  expect(Object.keys(spec.nodes)).toHaveLength(19);
  expect(spec).not.toHaveProperty("layout");
  expect(layout).toEqual({});
});

test("parseWorkflow reads a layout", () => {
  const text = "workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a}\n    layout:\n      a: {x: 10, y: 20}\n";
  expect(parseWorkflow(text).layout).toEqual({ a: { x: 10, y: 20 } });
});

test("parseWorkflow refuses text that is not exactly one workflow", () => {
  const two = "workflows:\n  a: {start: x, nodes: {x: {type: check, run: r}}}\n  b: {start: x, nodes: {x: {type: check, run: r}}}\n";
  expect(() => parseWorkflow(two)).toThrow(/one workflow/);
  expect(() => parseWorkflow("workflows: {}\n")).toThrow(/one workflow/);
  expect(() => parseWorkflow("nodes: []\n")).toThrow(/workflows/);
  expect(() => parseWorkflow("workflows:\n  w: {start: a, nodes: {a: {run: r}}}\n")).toThrow(/node a/);
  expect(() => parseWorkflow("workflows: [\n")).toThrow();
});

test.each([
  ["story", STORY_YAML],
  ["layer", LAYER_YAML],
])("the %s workflow round trips: the same nodes, in the same order, with the same fields", (_name, text) => {
  const before = parseWorkflow(text);
  const graph = autoLayout(fromWorkflow(before.name, before.spec, before.layout));
  const after = parseWorkflow(toYaml(graph));
  expect(after.name).toBe(before.name);
  expect(Object.keys(after.spec.nodes)).toEqual(Object.keys(before.spec.nodes));
  expect(after.spec.nodes).toEqual(before.spec.nodes);
  expect(after.spec.start).toBe(before.spec.start);
  expect(after.spec.type ?? null).toBe(before.spec.type ?? null);
  expect(after.spec.loose).toEqual(before.spec.loose);
  expect(Object.keys(after.layout)).toEqual(Object.keys(before.spec.nodes));
  expect(after.layout.intake ?? after.layout.covered).toEqual({ x: 0, y: 0 });
});

test("the text looks like the shipped files: type first, flow lists and maps, the layout last", () => {
  const { name, spec, layout } = parseWorkflow(STORY_YAML);
  const text = toYaml(autoLayout(fromWorkflow(name, spec, layout)));
  const lines = text.split("\n");
  expect(lines.slice(0, 5)).toEqual(["workflows:", "  story:", "    type: story", "    start: intake", "    loose: [card, repo]"]);
  expect(lines[5]).toBe("    nodes:");
  expect(text).toContain("        outputs: {base: json}\n");
  expect(text).toContain("        nodes: [refine, survey]\n");
  expect(text).toContain("        args: {stages: [survey, refine]}\n");
  expect(text).toContain("        role: [product, architect, tester]\n");
  expect(text).toContain("      intake: {x: 0, y: 0}\n");
  expect(text.indexOf("    layout:")).toBeGreaterThan(text.indexOf("      review:"));
});

test("node keys keep the file's order wherever the boxes sit: the order is what falls through to what", () => {
  const { name, spec } = parseWorkflow("workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a}\n      b: {type: check, run: x.b}\n      c: {type: check, run: x.c}\n");
  const graph = fromWorkflow(name, spec, { a: { x: 320, y: 0 }, b: { x: 0, y: 90 }, c: { x: 0, y: 0 } });
  const back = parseWorkflow(toYaml(graph));
  expect(Object.keys(back.spec.nodes)).toEqual(["a", "b", "c"]);
  expect(back.layout).toEqual({ a: { x: 320, y: 0 }, b: { x: 0, y: 90 }, c: { x: 0, y: 0 } });
});

test("a workflow without a type or loose names writes neither", () => {
  const { name, spec } = parseWorkflow("workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a}\n");
  const text = toYaml(fromWorkflow(name, spec, { a: { x: 0, y: 0 } }));
  expect(text).not.toContain("type: null");
  expect(text).not.toContain("loose");
  expect(text).toContain("    start: a\n");
});

test("a loop drawn on the canvas reparses with loop.to", () => {
  const text = "workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a}\n      b: {type: check, run: x.b, outputs: {notes: text}}\n    layout:\n      a: {x: 0, y: 0}\n      b: {x: 160, y: 0}\n";
  const { name, spec, layout } = parseWorkflow(text);
  const g = connect(fromWorkflow(name, spec, layout), "b", "a", "loop", { loop: { max: 3, carry: "notes" } });
  expect(parseWorkflow(toYaml(g)).spec.nodes.b.loop).toEqual({ to: "a", max: 3, carry: "notes" });
  expect(toYaml(g)).toContain("        loop: {to: a, max: 3, carry: notes}\n");
});

test("an empty canvas writes no start", () => {
  expect(toYaml(emptyGraph("w"))).not.toContain("start");
});

test("parseWorkflow refuses a node of an unknown type, naming it", () => {
  expect(() => parseWorkflow("workflows:\n  w: {start: a, nodes: {a: {type: shout, run: r}}}\n")).toThrow(/node a has unknown type 'shout'/);
});
