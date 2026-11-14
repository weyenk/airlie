import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { connect, emptyGraph, fromWorkflow, normalizeTopologyTypes } from "./graph";
import { autoLayout } from "./layout";
import { parseWorkflow, toYaml } from "./yaml";

test("parseWorkflow reads the one workflow in a file: its name, type, inputs, nodes and (absent) layout", () => {
  const { name, spec, layout } = parseWorkflow(STORY_YAML);
  expect(name).toBe("story");
  expect(spec).toMatchObject({ type: "story", inputs: ["card", "repo"], outputs: [] });
  expect(Object.keys(spec.nodes)).toHaveLength(22);
  expect(spec.nodes.to_build.to).toEqual([{ node: "gate", when: "to_build" }, { node: "request", when: "config.story.survey" }, "story_card"]);
  expect(spec).not.toHaveProperty("layout");
  expect(layout).toEqual({});
  expect(parseWorkflow(LAYER_YAML).spec).toMatchObject({ inputs: ["card", "repo", "story", "layers", "base", "gate", "agreement"], outputs: ["drafted", "built.{item}", "satisfied.{item}"] });
});

test("parseWorkflow reads a layout", () => {
  const text = "workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a}\n    layout:\n      a: {x: 10, y: 20}\n";
  expect(parseWorkflow(text).layout).toEqual({ a: { x: 10, y: 20 } });
});

test("parseWorkflow refuses text that is not exactly one workflow", () => {
  const two = "workflows:\n  a: {nodes: {x: {type: check, run: r}}}\n  b: {nodes: {x: {type: check, run: r}}}\n";
  expect(() => parseWorkflow(two)).toThrow(/one workflow/);
  expect(() => parseWorkflow("workflows: {}\n")).toThrow(/one workflow/);
  expect(() => parseWorkflow("nodes: []\n")).toThrow(/workflows/);
  expect(() => parseWorkflow("workflows:\n  w: {nodes: {a: {run: r}}}\n")).toThrow(/node a/);
  expect(() => parseWorkflow("workflows: [\n")).toThrow();
});

test("parseWorkflow refuses a node of an unknown type, naming it", () => {
  expect(() => parseWorkflow("workflows:\n  w: {nodes: {a: {type: shout, run: r}}}\n")).toThrow(/node a has unknown type 'shout'/);
});

test("parseWorkflow refuses the old schema as the server does: start, loose, next, branch, loop, parallel, each", () => {
  expect(() => parseWorkflow("workflows:\n  w: {start: a, nodes: {a: {type: check, run: r}}}\n")).toThrow(/field 'start' is the old schema/);
  expect(() => parseWorkflow("workflows:\n  w: {loose: [card], nodes: {a: {type: check, run: r}}}\n")).toThrow(/field 'loose' is the old schema/);
  expect(() => parseWorkflow("workflows:\n  w: {nodes: {a: {type: check, run: r, next: b}}}\n")).toThrow(/node a: field 'next' is the old schema/);
  expect(() => parseWorkflow("workflows:\n  w: {nodes: {a: {type: check, run: r, on_fail: {to: a, max: 2}}}}\n")).toThrow(/node a: field 'on_fail' is the old schema/);
  expect(() => parseWorkflow("workflows:\n  w: {nodes: {a: {type: parallel, nodes: []}}}\n")).toThrow(/node a: type 'parallel' is the old schema/);
  expect(() => parseWorkflow("workflows:\n  w: {nodes: {a: {type: each, over: x, workflow: l}}}\n")).toThrow(/node a: type 'each' is the old schema/);
});

test("parseWorkflow refuses a line without a node", () => {
  expect(() => parseWorkflow("workflows:\n  w: {nodes: {a: {type: check, run: r, to: [{when: x}]}}}\n")).toThrow(/node a: a to line needs a node/);
  expect(() => parseWorkflow("workflows:\n  w: {nodes: {a: {type: check, run: r, to: b}}}\n")).toThrow(/node a: to must be a list/);
});

test("parseWorkflow refuses a loop line without its max, naming the node and the line", () => {
  const bad = (loop: string) => `workflows:\n  w: {nodes: {a: {type: check, run: r}, b: {type: check, run: r, to: [{node: a, loop: ${loop}}]}}}\n`;
  expect(() => parseWorkflow(bad("{carry: x}"))).toThrow(/workflow w: node b: the line to a: loop needs max/);
  expect(() => parseWorkflow(bad("{max: ''}"))).toThrow(/loop needs max/);
  expect(() => parseWorkflow(bad("3"))).toThrow(/loop needs max/);
  expect(parseWorkflow(bad("{max: 2}")).spec.nodes.b.to).toEqual([{ node: "a", loop: { max: 2 } }]);
});

test("a loop without its max is never written as an empty max", () => {
  const { name, spec, layout } = parseWorkflow("workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a}\n");
  const g = fromWorkflow(name, spec, layout);
  const text = toYaml({ ...g, edges: [{ from: "a", to: "a", artifacts: [], loop: { max: "" } }] });
  expect(text).toContain("        to: [a]\n");
  expect(text).not.toContain("max");
});

test.each([
  ["story", STORY_YAML],
  ["layer", LAYER_YAML],
])("the shipped %s workflow round trips: the same nodes, in the same order, with the same fields and lines", (_name, text) => {
  const before = parseWorkflow(text);
  const graph = autoLayout(fromWorkflow(before.name, before.spec, before.layout));
  const after = parseWorkflow(toYaml(graph));
  expect(after.name).toBe(before.name);
  expect(Object.keys(after.spec.nodes)).toEqual(Object.keys(before.spec.nodes));
  expect(after.spec.nodes).toEqual(normalizeTopologyTypes(before.spec).nodes);
  expect(after.spec.type ?? null).toBe(before.spec.type ?? null);
  expect(after.spec.inputs).toEqual(before.spec.inputs);
  expect(after.spec.outputs).toEqual(before.spec.outputs);
  expect(Object.keys(after.layout)).toEqual(Object.keys(before.spec.nodes));
});

test("the text looks like the shipped files: type and inputs first, flow lists and maps, a line per row, the layout last", () => {
  const { name, spec, layout } = parseWorkflow(STORY_YAML);
  const text = toYaml(autoLayout(fromWorkflow(name, spec, layout)));
  const lines = text.split("\n");
  expect(lines.slice(0, 5)).toEqual(["workflows:", "  story:", "    type: story", "    inputs: [card, repo]", "    nodes:"]);
  expect(text).toContain("        outputs: {base: json}\n");
  expect(text).toContain("        outputs: {to_build: {format: bool, transient: true}}\n");
  expect(text).toContain("        to: [base]\n");
  expect(text).toContain("        to:\n          - {node: gate, when: to_build}\n          - {node: request, when: config.story.survey}\n          - story_card\n");
  expect(text).toContain("        args: {stages: [survey, refine]}\n");
  expect(text).toContain("        events: {done: survey_done, failed: survey_failed}\n");
  expect(text).toContain("        role: [product, architect]\n");
  expect(text).toContain("      intake: {x: 0, y: 0}\n");
  expect(text.indexOf("    layout:")).toBeGreaterThan(text.indexOf("      review:"));
  expect(text).not.toContain("start");
});

test("the layer's text: outputs declared on the workflow, item-scoped names quoted, a loop line with its bound", () => {
  const { name, spec, layout } = parseWorkflow(LAYER_YAML);
  const text = toYaml(fromWorkflow(name, spec, layout));
  expect(text).toContain('    outputs: [drafted, "built.{item}", "satisfied.{item}"]\n');
  expect(text).toContain("          - {node: package, loop: {max: story.layer_attempts, carry: feedback}}\n");
});

test("node keys keep the file's order wherever the boxes sit", () => {
  const { name, spec } = parseWorkflow("workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a}\n      b: {type: check, run: x.b}\n      c: {type: check, run: x.c}\n");
  const graph = fromWorkflow(name, spec, { a: { x: 320, y: 0 }, b: { x: 0, y: 90 }, c: { x: 0, y: 0 } });
  const back = parseWorkflow(toYaml(graph));
  expect(Object.keys(back.spec.nodes)).toEqual(["a", "b", "c"]);
  expect(back.layout).toEqual({ a: { x: 320, y: 0 }, b: { x: 0, y: 90 }, c: { x: 0, y: 0 } });
});

test("a workflow without a type, inputs or outputs writes none of them", () => {
  const { name, spec } = parseWorkflow("workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a}\n");
  const text = toYaml(fromWorkflow(name, spec, { a: { x: 0, y: 0 } }));
  expect(text).toBe("workflows:\n  w:\n    nodes:\n      a:\n        type: check\n        run: x.a\n    layout:\n      a: {x: 0, y: 0}\n");
});

test("a line drawn on the canvas reparses as a to entry", () => {
  const text = "workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a}\n      b: {type: check, run: x.b, outputs: {notes: text}}\n";
  const { name, spec, layout } = parseWorkflow(text);
  let g = connect(fromWorkflow(name, spec, layout), "b", "a", { loop: { max: 3, carry: "notes" } });
  g = connect(g, "a", "b", {});
  const back = parseWorkflow(toYaml(g)).spec.nodes;
  expect(back.b.to).toEqual([{ node: "a", loop: { max: 3, carry: "notes" } }]);
  expect(back.a.to).toEqual(["b"]);
});

test("an empty canvas writes the new canvas's inputs and no nodes", () => {
  expect(toYaml(emptyGraph("w"))).toBe("workflows:\n  w:\n    inputs: [card, repo]\n    nodes: {}\n    layout: {}\n");
});
