import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { fromWorkflow } from "./graph";
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

test("node keys follow the canvas: left to right, then top to bottom", () => {
  const { name, spec } = parseWorkflow("workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a}\n      b: {type: check, run: x.b}\n      c: {type: check, run: x.c}\n");
  const graph = fromWorkflow(name, spec, { a: { x: 320, y: 0 }, b: { x: 0, y: 90 }, c: { x: 0, y: 0 } });
  expect(Object.keys(parseWorkflow(toYaml(graph)).spec.nodes)).toEqual(["c", "b", "a"]);
});

test("a workflow without a type or loose names writes neither", () => {
  const { name, spec } = parseWorkflow("workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a}\n");
  const text = toYaml(fromWorkflow(name, spec, { a: { x: 0, y: 0 } }));
  expect(text).not.toContain("type: null");
  expect(text).not.toContain("loose");
  expect(text).toContain("    start: a\n");
});
