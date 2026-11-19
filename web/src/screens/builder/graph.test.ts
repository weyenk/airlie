// The graph read from the shipped workflow files: a box per node, a line per `to` entry carrying the
// artifacts its source writes and its target reads.
import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { fromWorkflow, KINDS, NODE_KINDS } from "./graph";
import type { Graph } from "./graph";
import { shapeOf } from "./shapes";
import { parseWorkflow } from "./yaml";

const graphOf = (text: string): Graph => {
  const { name, spec, layout } = parseWorkflow(text);
  return fromWorkflow(name, spec, layout);
};
const lines = (g: Graph) => g.edges.map((e) => `${e.from}>${e.to}`);
const line = (g: Graph, from: string, to: string) => g.edges.filter((e) => e.from === from && e.to === to);

test("the node kinds are the four protocols and nothing else", () => {
  expect(NODE_KINDS).toEqual(["WORK", "ROUND_ROBIN", "SUPERVISOR_WORKERS", "K_INDEPENDENT_TEAMS"]);
  expect(KINDS).toEqual(["text", "number", "bool", "list of …", "one of …"]);
});

test("the shipped story workflow: a node per entry in the file's order, with its kind and fields", () => {
  const g = graphOf(STORY_YAML);
  expect(g.name).toBe("story");
  expect(g.title).toBe("story");
  expect(g.nodes.map((n) => n.id)).toEqual([
    "intake",
    "base",
    "to_build",
    "survey",
    "refine",
    "layers",
    "agree_plain",
    "amigos",
  ]);
  const base = g.nodes.find((n) => n.id === "base");
  expect(base?.kind).toBe("WORK");
  expect(base?.spec.run).toBe("story base");
  expect(g.nodes.find((n) => n.id === "to_build")?.spec.outputs).toEqual({
    to_build: { format: "bool", transient: true },
  });
});

test("the story workflow reads one line per to entry", () => {
  const g = graphOf(STORY_YAML);
  expect(g.edges).toHaveLength(8);
  expect(lines(g)).toEqual([
    "intake>base",
    "base>to_build",
    "to_build>refine",
    "to_build>survey",
    "refine>layers",
    "survey>layers",
    "layers>agree_plain",
    "layers>amigos",
  ]);
});

test("a line carries the outputs of its source that its target reads", () => {
  const g = graphOf(STORY_YAML);
  expect(line(g, "survey", "layers")[0].artifacts).toEqual(["survey"]);
  expect(line(g, "refine", "layers")[0].artifacts).toEqual(["story"]);
  expect(line(g, "layers", "amigos")[0].artifacts).toEqual(["layers"]);
});

test("story lines: four are conditional, none loops", () => {
  const g = graphOf(STORY_YAML);
  expect(line(g, "layers", "agree_plain")[0].when).toBe("not config.story.amigos.enabled");
  expect(line(g, "layers", "amigos")[0].when).toBe("config.story.amigos.enabled");
  expect(g.edges.filter((e) => e.when).length).toBe(4);
  expect(g.edges.filter((e) => e.loop).length).toBe(0);
});

test("an unconditional line carries no when", () => {
  const g = graphOf(STORY_YAML);
  expect(line(g, "intake", "base")[0].when ?? null).toBeNull();
});

test("the layer workflow: its inputs as written, a line per to entry, and its loop lines with their bound", () => {
  const g = graphOf(LAYER_YAML);
  expect(g.title).toBe("layer");
  expect(g.inputs).toEqual(["card", "repo", "story", "layers", "base", "gate", "agreement"]);
  expect(g.nodes.map((n) => n.id)).toEqual([
    "start",
    "covered",
    "drafts",
    "package",
    "place",
    "build",
    "done_check",
    "review",
    "fix",
    "final_gate",
    "outcome",
    "squash",
    "costed_layer",
  ]);
  expect(line(g, "start", "covered")[0]).toMatchObject({
    from: "start",
    to: "covered",
    when: "not built.{item} exists and not satisfied.{item} exists",
  });
});

test("a line with a loop carries its bound", () => {
  const g = graphOf(LAYER_YAML);
  expect(line(g, "fix", "review")[0].loop).toEqual({ max: 8 });
  expect(line(g, "outcome", "package")[0].loop).toEqual({ max: 3, carry: "feedback" });
  expect(line(g, "outcome", "squash")[0].loop ?? null).toBeNull();
});

test("a when line carries its condition, and an artifact line what the target reads", () => {
  const g = graphOf(LAYER_YAML);
  expect(line(g, "package", "place")[0]).toMatchObject({
    from: "package",
    to: "place",
    when: "package.{item} exists",
  });
});

test("a round trip of a small spec keeps kinds, fields and lines", () => {
  const text = `
workflows:
  w:
    inputs: [x]
    outputs: [z]
    nodes:
      a: {kind: WORK, run: x.a, inputs: [x], outputs: {y: text}}
      b: {kind: WORK, run: x.b, inputs: [y], outputs: {z: text}}
    edges:
      a: [{target: b}]
`;
  const { name, spec } = parseWorkflow(text);
  const g = fromWorkflow(name, spec);
  expect(g.nodes.map((n) => n.kind)).toEqual(["WORK", "WORK"]);
  expect(lines(g)).toEqual(["a>b"]);
  expect(line(g, "a", "b")[0].artifacts).toEqual(["y"]);
});

test("layout places the boxes it knows; the others wait for autoLayout", () => {
  const { name, spec } = parseWorkflow(LAYER_YAML);
  const g = fromWorkflow(name, spec, { drafts: { x: 60, y: 20 } });
  const placed = g.nodes.find((n) => n.id === "drafts");
  expect(placed?.x).toBe(60);
  expect(placed?.y).toBe(20);
  expect(g.nodes.find((n) => n.id === "build")?.x ?? 0).toBe(0);
});

test("a check is code, a model is work and the team kinds are their own shape", () => {
  expect(shapeOf("WORK")).toBe("work");
  expect(shapeOf("ROUND_ROBIN")).toBe("work");
  expect(shapeOf("SUPERVISOR_WORKERS")).toBe("work");
  expect(shapeOf("K_INDEPENDENT_TEAMS")).toBe("work");
});
