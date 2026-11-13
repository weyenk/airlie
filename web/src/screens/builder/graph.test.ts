// The graph read from the shipped workflow files (schema v2): a box per node, a line per `to` entry carrying the
// artifacts its source writes and its target reads.
import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { fromWorkflow, KINDS, NODE_TYPES } from "./graph";
import type { Graph } from "./graph";
import { SHAPE, shapeOf } from "./shapes";
import { parseWorkflow } from "./yaml";

const graphOf = (text: string): Graph => {
  const { name, spec, layout } = parseWorkflow(text);
  return fromWorkflow(name, spec, layout);
};
const line = (g: Graph, from: string, to: string) => g.edges.filter((e) => e.from === from && e.to === to);
const lines = (g: Graph) => g.edges.map((e) => `${e.from}>${e.to}`);

test("the node types are schema v2's: no parallel or each, a team and a bundle", () => {
  expect(NODE_TYPES).toEqual(["model", "ask", "conversation", "write", "meeting", "team", "check", "gate", "bundle"]);
  expect(KINDS).toEqual(["text", "number", "bool", "list of …", "one of …"]);
});

test("the shipped story workflow: a node per entry, in the file's order, with its type, inputs and fields", () => {
  const g = graphOf(STORY_YAML);
  expect(g.name).toBe("story");
  expect(g.type).toBe("story");
  expect(g.inputs).toEqual(["card", "repo"]);
  expect(g.outputs).toEqual([]);
  expect(g.nodes.map((n) => n.id)).toEqual([
    "intake", "base", "to_build", "gate", "request", "story_card", "take_card_as_story", "refine", "survey", "survey_note", "costed_refine",
    "layers", "costed_plan", "agree_plain", "amigos", "costed_amigos", "planned", "build_layers", "built", "publish", "costed_publish", "review",
  ]);
  const base = g.nodes.find((n) => n.id === "base")!;
  expect(base).toMatchObject({ type: "check", spec: { run: "story.base", inputs: ["card", "repo"], outputs: { base: "json" } } });
  expect(base.spec).not.toHaveProperty("type");
  expect(base.spec).not.toHaveProperty("to"); // the lines are the edges
  expect(g.nodes.find((n) => n.id === "to_build")!.spec.outputs).toEqual({ to_build: { format: "bool", transient: true } });
});

test("a line per to entry, in the file's order", () => {
  const g = graphOf(STORY_YAML);
  expect(g.edges).toHaveLength(30);
  expect(lines(g).slice(0, 9)).toEqual([
    "intake>base", "base>to_build", "base>build_layers", "to_build>gate", "to_build>request", "to_build>story_card",
    "gate>request", "gate>story_card", "gate>build_layers",
  ]);
  expect(g.edges.filter((e) => e.from === "review")).toEqual([]);
});

test("a line carries the outputs of its source that its target reads", () => {
  const g = graphOf(STORY_YAML);
  expect(line(g, "survey", "survey_note")).toEqual([{ from: "survey", to: "survey_note", artifacts: ["survey"] }]);
  expect(line(g, "base", "build_layers")[0].artifacts).toEqual(["base"]);
  expect(line(g, "gate", "build_layers")[0].artifacts).toEqual(["gate"]);
  // in the source's order, every output the bundle reads
  expect(line(g, "amigos", "build_layers")[0].artifacts).toEqual(["agreement", "story", "layers"]);
});

test("a line that carries nothing is an ordering line", () => {
  const g = graphOf(STORY_YAML);
  expect(line(g, "intake", "base")).toEqual([{ from: "intake", to: "base", artifacts: [] }]);
  expect(line(g, "base", "to_build")[0].artifacts).toEqual([]); // to_build reads no base
  expect(line(g, "request", "story_card")[0].artifacts).toEqual([]);
});

test("a condition is the line's when; two lines out of one node are two edges", () => {
  const g = graphOf(STORY_YAML);
  expect(line(g, "to_build", "gate")).toEqual([{ from: "to_build", to: "gate", artifacts: [], when: "to_build" }]);
  expect(line(g, "to_build", "story_card")[0]).not.toHaveProperty("when");
  expect(line(g, "story_card", "refine")).toEqual([{ from: "story_card", to: "refine", artifacts: [], when: "not is_story_card" }]);
  expect(line(g, "story_card", "survey")).toEqual([
    { from: "story_card", to: "survey", artifacts: [], when: "not is_story_card and config.story.survey and not survey exists and not story exists" },
  ]);
  expect(line(g, "gate", "request")).toHaveLength(1);
  expect(line(g, "gate", "story_card")).toHaveLength(1);
});

test("the survey node is a model with protocol ask", () => {
  const n = graphOf(STORY_YAML).nodes.find((x) => x.id === "survey");
  expect(n).toMatchObject({ type: "model", spec: { protocol: "ask", role: "architect", prompt: "prompt:story-survey" } });
});

test("build_layers is a bundle node naming its workflow and the list it runs over", () => {
  const n = graphOf(STORY_YAML).nodes.find((x) => x.id === "build_layers");
  expect(n).toMatchObject({ type: "bundle", spec: { workflow: "layer", each: "layers", outputs: { built: "json", satisfied: "json" } } });
});

test("the layer bundle: its inputs and outputs, item-scoped artifacts as written, and a loop line with its bound", () => {
  const g = graphOf(LAYER_YAML);
  expect(g.type).toBeNull();
  expect(g.inputs).toEqual(["card", "repo", "story", "layers", "base", "gate", "agreement"]);
  expect(g.outputs).toEqual(["drafted", "built.{item}", "satisfied.{item}"]);
  expect(line(g, "package", "place")).toEqual([{ from: "package", to: "place", artifacts: ["package.{item}"], when: "package.{item} exists and not satisfied.{item} exists" }]);
  expect(line(g, "drafts", "package")[0].artifacts).toEqual(["drafts_session"]);
  expect(line(g, "place", "build")).toEqual([{ from: "place", to: "build", artifacts: ["studio.{item}"] }]);
  expect(line(g, "outcome", "package")).toEqual([{ from: "outcome", to: "package", artifacts: [], loop: { max: "story.layer_attempts", carry: "feedback" } }]);
  expect(line(g, "squash", "costed_layer")).toEqual([{ from: "squash", to: "costed_layer", artifacts: [] }]);
});

test("a target reading a name its source does not write gets an ordering line, no error", () => {
  const g = graphOf("workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a, outputs: {x: text}, to: [b]}\n      b: {type: check, run: x.b, inputs: [y]}\n");
  expect(g.edges).toEqual([{ from: "a", to: "b", artifacts: [] }]);
});

test("a given layout places the boxes; the others wait for autoLayout", () => {
  const { name, spec } = parseWorkflow(LAYER_YAML);
  const g = fromWorkflow(name, spec, { build: { x: 300, y: 40 } });
  expect(g.nodes.find((n) => n.id === "build")).toMatchObject({ x: 300, y: 40, placed: true });
  expect(g.nodes.find((n) => n.id === "start")).toMatchObject({ placed: false });
});

test("shapes: a model step, a team too; a check is code; a gate a gate; a bundle a bundle", () => {
  expect(shapeOf("bundle")).toBe("bundle");
  expect(shapeOf("team")).toBe("model");
  expect(shapeOf("meeting")).toBe("model");
  expect(shapeOf("check")).toBe("code");
  expect(shapeOf("gate")).toBe("gate");
  expect(Object.keys(SHAPE)).toEqual([...NODE_TYPES]);
});
