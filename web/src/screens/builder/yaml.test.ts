import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { fromWorkflow } from "./graph";
import { parseWorkflow, toYaml } from "./yaml";

/** One workflow's file: a flow mapping is still YAML, and JSON is a flow mapping. */
const workflowText = (name: string, body: Record<string, unknown>): string =>
  `workflows:\n  ${name}: ${JSON.stringify(body)}\n`;

const twoNodes = { a: { kind: "WORK" }, b: { kind: "WORK" } };

test("parseWorkflow reads one workflow's file: its name, title, inputs, nodes and edges", () => {
  const { name, spec, layout } = parseWorkflow(STORY_YAML);
  expect(name).toBe("story");
  expect(spec.title).toBe("story");
  expect(spec.inputs).toEqual(["card", "repo"]);
  expect(spec.nodes.intake).toMatchObject({ kind: "WORK", run: "story intake" });
  expect(spec.nodes.base).toMatchObject({ kind: "WORK", outputs: { base: "json" } });
  expect((spec.edges?.base[0] as Record<string, unknown>).target).toBe("to_build");
  expect((spec.edges?.layers[1] as Record<string, unknown>).when).toBe("config.story.amigos.enabled");
  expect(layout).toEqual({});
});

test("parseWorkflow reads a layout: the saved point, and no entry where one is missing", () => {
  const { spec, layout } = parseWorkflow(
    workflowText("l", { nodes: twoNodes, layout: { a: { x: 10, y: 20 }, b: { x: "wide", y: 3 } } }),
  );
  expect(spec.nodes.b).toBeDefined();
  expect(layout).toEqual({ a: { x: 10, y: 20 } });
});

test("parseWorkflow refuses a file that does not hold exactly one workflow", () => {
  expect(() => parseWorkflow("workflows:\n  a: {nodes: {}}\n  b: {nodes: {}}\n")).toThrow(
    /must hold one workflow, not 2/,
  );
  expect(() => parseWorkflow("workflows: {}\n")).toThrow(/must hold one workflow, not 0/);
});

test("parseWorkflow refuses a field the server does not know, naming it", () => {
  expect(() => parseWorkflow(workflowText("w", { start: "a", nodes: {} }))).toThrow(
    /unknown field 'start'/,
  );
  expect(() => parseWorkflow(workflowText("w", { parallel: true, nodes: {} }))).toThrow(
    /unknown field 'parallel'/,
  );
});

test("parseWorkflow refuses a node with no kind or one it does not know, naming it", () => {
  expect(() => parseWorkflow(workflowText("r", { nodes: { a: {} } }))).toThrow(/node a has no kind/);
  expect(() =>
    parseWorkflow(workflowText("r", { nodes: { a: { kind: "shout" } } })),
  ).toThrow(/node a has unknown kind 'shout'/);
});

test("parseWorkflow refuses an edge with no target, a loop with no max and edges that are not a list", () => {
  expect(() =>
    parseWorkflow(workflowText("e", { nodes: twoNodes, edges: { a: [{ when: "x" }] } })),
  ).toThrow(/edge from a needs a target/);
  expect(() =>
    parseWorkflow(workflowText("e", { nodes: twoNodes, edges: { a: [{ target: "b", loop: {} }] } })),
  ).toThrow(/edge from a: loop needs max/);
  expect(() => parseWorkflow(workflowText("e", { nodes: twoNodes, edges: { a: "b" } }))).toThrow(
    /edges\.a must be a list/,
  );
});

test("parseWorkflow keeps what it does not model: node fields and edge line keys", () => {
  const { spec } = parseWorkflow(
    workflowText("k", {
      nodes: { a: { kind: "WORK", run: "r", inputs: ["x"], outputs: ["y"], max: 3 } },
      edges: {},
    }),
  );
  expect(spec.nodes.a).toEqual({ kind: "WORK", run: "r", inputs: ["x"], outputs: ["y"], max: 3 });
});

/** A line with nothing but a target is the same wire however it is spelled. */
const normEdges = (edges: Record<string, unknown[]> | undefined): Record<string, unknown[]> =>
  Object.fromEntries(
    Object.entries(edges ?? {}).map(([from, lines]) => [
      from,
      (lines as Record<string, unknown>[]).map((line) =>
        typeof line === "object" && line !== null && Object.keys(line).length === 1
          ? line.target
          : line,
      ),
    ]),
  );

test.each([
  ["story", STORY_YAML],
  ["layer", LAYER_YAML],
])("the shipped %s workflow round trips: parse, redraw and save gives back the same nodes, lines and points", (_name, text) => {
  const before = parseWorkflow(text);
  const graph = fromWorkflow(before.name, before.spec, before.layout);
  const saved = toYaml(graph);
  const after = parseWorkflow(saved);
  expect(after.name).toBe(before.name);
  expect(after.spec.nodes).toEqual(before.spec.nodes);
  expect(normEdges(after.spec.edges)).toEqual(normEdges(before.spec.edges));
  // saving again is stable: the canvas's output is a fixed point
  expect(toYaml(fromWorkflow(after.name, after.spec, after.layout))).toBe(saved);
});

test("toYaml keeps the node order and writes title, nodes, edges and layout in order", () => {
  const { name, spec, layout } = parseWorkflow(
    workflowText("w", {
      title: "order test",
      nodes: { b: { kind: "ROUND_ROBIN", run: "go" }, a: { kind: "WORK" } },
      edges: { b: [{ target: "a", when: "done" }] },
      layout: { a: { x: 1, y: 2 } },
    }),
  );
  expect(Object.keys(spec.nodes)).toEqual(["b", "a"]);
  const text = toYaml(fromWorkflow(name, spec, layout));
  expect(text.indexOf("title:")).toBeLessThan(text.indexOf("nodes:"));
  expect(text.indexOf("nodes:")).toBeLessThan(text.indexOf("edges:"));
  expect(text.indexOf("edges:")).toBeLessThan(text.indexOf("layout:"));
});

test("toYaml writes no empty lists or keys", () => {
  const text = toYaml(
    fromWorkflow("e", { inputs: [], outputs: [], nodes: twoNodes, edges: {} }, {}),
  );
  expect(text).toContain("nodes:\n      a:\n        kind: WORK");
  expect(text).not.toContain("inputs: []");
  expect(text).not.toContain("outputs: {}");
  expect(text).not.toContain("edges: {}");
  expect(text).not.toContain("layout: {}");
});

test("a line with a condition but no bound writes no empty loop", () => {
  const { name, spec, layout } = parseWorkflow(
    workflowText("w", { nodes: twoNodes, edges: { a: [{ target: "a", when: "done" }] } }),
  );
  const text = toYaml(fromWorkflow(name, spec, layout));
  expect(text).toContain("when: done");
  expect(text).not.toContain("loop");
});

test("a field the builder does not model survives the round trip", () => {
  const text = workflowText("w", { nodes: { a: { kind: "WORK", cost: 4 } }, edges: {} });
  const { name, spec, layout } = parseWorkflow(text);
  expect(toYaml(fromWorkflow(name, spec, layout))).toContain("cost: 4");
});
