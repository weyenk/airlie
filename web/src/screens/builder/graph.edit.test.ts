// The graph's editing functions (graph.ts) on schema v2: a line is a `to` entry, what it carries is what its source
// writes and its target reads, and every edit keeps the node fields and the lines in step.
import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import {
  addNode,
  withBundle,
  bundleNames,
  connect,
  emptyGraph,
  fromWorkflow,
  moveNode,
  portsOf,
  producersOf,
  readable,
  removeEdge,
  removeNode,
  renameInCondition,
  renameNode,
  renameOutput,
  toWorkflow,
  updateEdge,
  updateNode,
  
} from "./graph";
import type { Graph } from "./graph";
import { parseWorkflow } from "./yaml";

/** a writes notes and verdict and goes on to b (which reads notes); c reads nothing yet; d writes notes too. */
const SMALL = `workflows:
  w:
    inputs: [card]
    nodes:
      a: {kind: WORK, run: x.a, inputs: [card], outputs: {verdict: text, notes: markdown}}
      b: {kind: WORK, prompt: "Fix {notes}; {?notes}again {notes.why}{/notes}", inputs: [notes]}
      c: {kind: WORK, run: x.c}
      d: {kind: WORK, run: x.d, outputs: {notes: markdown}}
    edges:
      a: [b]
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
  expect(toWorkflow(g).spec.edges.a).toEqual(["b", "c"]);
});

test("connect adds an input once: a second line between the same nodes is merged into the first", () => {
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
  expect(toWorkflow(g).spec.edges.b).toEqual([{ target: "a", loop: { max: 3, carry: "notes" } }]);
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

test("renameOutput follows every reader of the output into its new name", () => {
  const text = `workflows:
  w:
    nodes:
      p: {kind: WORK, outputs: {plan: text}}
      c: {kind: WORK, prompt: "go on", inputs: [plan]}
      d: {kind: WORK, inputs: [plan]}
    edges:
      p: [c, d]
`;
  const g = renameOutput(graphOf(text), "p", "plan", "talk");
  if ("refused" in g) throw new Error("refused");
  expect(specOf(g, "c").inputs).toEqual(["talk"]);
  expect(specOf(g, "c").prompt).toBe("go on");
  expect(specOf(g, "d").inputs).toEqual(["talk"]);
});

test("renameInCondition rewrites the paths headed by the name, never a keyword, a literal or another path", () => {
  expect(renameInCondition("is_story_card", "is_story_card", "story_now")).toBe("story_now");
  expect(renameInCondition("not story exists and config.story.survey", "story", "tale")).toBe("not tale exists and config.story.survey");
  expect(renameInCondition('story.kind == story and storyline != "story"', "story", "tale")).toBe('tale.kind == story and storyline != "story"');
  expect(renameInCondition("not satisfied.{item} exists", "satisfied.{item}", "covered.{item}")).toBe("not covered.{item} exists");
  expect(renameInCondition("(verdict.decision==pass) or not verdict", "verdict", "v")).toBe("(v.decision==pass) or not v");
});

test("renameOutput follows the output into a condition, not just into lines", () => {
  const g = renameOutput(graphOf(STORY_YAML), "to_build", "to_build", "ready_to_build");
  if ("refused" in g) throw new Error("refused");
  const spec = toWorkflow(g).spec;
  expect(spec.edges.to_build).toEqual([
    { target: "refine", when: "ready_to_build" },
    { target: "survey", when: "ready_to_build" },
  ]);
  expect(spec.nodes.to_build.outputs).toEqual({ ready_to_build: { format: "bool", transient: true } });
});

test("renameOutput follows an item-scoped output into every line's condition that names it", () => {
  const layer = graphOf(LAYER_YAML);
  // satisfied.{item} has a single writer, so every condition naming it follows the rename
  const g = renameOutput(layer, "covered", "satisfied.{item}", "covered.{item}");
  if ("refused" in g) throw new Error("refused");
  expect(lineOf(g, "covered", "drafts")[0].when).toBe("not covered.{item} exists");
  expect(lineOf(g, "covered", "package")[0].when).toBe("not covered.{item} exists");
  // lines that are not the renamed box's own keep the name
  expect(lineOf(g, "start", "covered")[0].when).toBe("not built.{item} exists and not satisfied.{item} exists");
  // a condition that never names the output is left alone
  expect(lineOf(g, "package", "place")[0].when).toBe("package.{item} exists");
});


test("renameOutput refuses a name taken or an output that is not there", () => {
  expect(() => renameOutput(small(), "a", "notes", "verdict")).toThrow(/already/);
  expect(() => renameOutput(small(), "a", "nope", "x")).toThrow(/nope/);
});

test("renameOutput of an output the whole workflow writes renames it there too", () => {
  const g = renameOutput({ ...small(), outputs: ["notes"] }, "a", "notes", "remarks");
  if ("refused" in g) throw new Error("refused");
  expect(g.outputs).toEqual(["remarks"]);
});

test("addNode gives each kind its default fields, names it <kind>-N and puts it last, with no line", () => {
  let g = addNode(small(), "WORK", 480, 0);
  expect(g.nodes.at(-1)).toMatchObject({ id: "work-1", kind: "WORK", spec: { prompt: "", inputs: [], outputs: {} }, x: 480, y: 0, placed: true });
  expect(g.edges).toEqual(small().edges);
  const blank = (type: Parameters<typeof addNode>[1]) => specOf(addNode(emptyGraph("w"), type, 0, 0), `${type.toLowerCase()}-1`);
  expect(blank("WORK")).toEqual({ prompt: "", inputs: [], outputs: {} });
  expect(blank("ROUND_ROBIN")).toEqual({ roles: [], prompt: "", agreement_when: "", inputs: [], outputs: {} });
  expect(blank("SUPERVISOR_WORKERS")).toEqual({ supervisor: "", worker: "", workers: 3, prompt: "", inputs: [], outputs: {} });
  expect(blank("K_INDEPENDENT_TEAMS")).toEqual({ teams: 3, roles: [""], prompt: "", inputs: [], outputs: {}, aggregate: "llm" });
  // the caller hands in what the catalog says (a meeting's seats and inputs)
  g = addNode(emptyGraph("w"), "ROUND_ROBIN", 0, 0, { roles: ["product", "architect", "tester"], inputs: ["story", "layers"] });
  expect(specOf(g, "round_robin-1")).toEqual({ roles: ["product", "architect", "tester"], prompt: "", agreement_when: "", inputs: ["story", "layers"], outputs: {} });
  g = addNode(addNode(addNode(g, "WORK", 0, 0), "WORK", 0, 0), "WORK", 0, 0);
  expect(addNode(removeNode(g, "work-2"), "WORK", 0, 0).nodes.map((n) => n.id)).toEqual(["round_robin-1", "work-1", "work-3", "work-2"]);
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
  expect(spec.nodes.a).toMatchObject({ kind: "WORK", run: "x.a" });
  expect(spec.nodes.a).not.toHaveProperty("to");
  expect(spec.edges.a).toEqual(["b", { target: "c", when: "verdict == yes" }]);
  expect(layout.a).toEqual({ x: 480, y: 0 });
  expect(toWorkflow(emptyGraph("new")).spec).toEqual({ inputs: ["card", "repo"], outputs: [], nodes: {}, edges: {} });
});

test("connect merges into the line between a pair: artifacts join, when and loop stay", () => {
  let g = connect(small(), "a", "c", { artifacts: ["verdict"], when: "verdict == yes" });
  g = connect(g, "a", "c", { artifacts: ["notes"] });
  expect(lineOf(g, "a", "c")).toEqual([{ from: "a", to: "c", artifacts: ["verdict", "notes"], when: "verdict == yes" }]);
  g = connect(g, "a", "c", { artifacts: ["notes"], when: "verdict == no" });
  expect(lineOf(g, "a", "c")[0].when).toBe("verdict == no");
});


test("producersOf names the boxes that read a name, so a line's removal knows who loses it", () => {
  const layer = graphOf(LAYER_YAML);
  expect(lineOf(layer, "start", "covered")).toHaveLength(1);
  // d writes notes as well as a, but the readers of notes are what a line to b protects.
  expect(producersOf(small(), "notes", "a", "b")).toEqual(["b"]);
});

test("updateEdge replaces what a line carries, and sets or clears its when and loop", () => {
  let g = connect(small(), "a", "c", { artifacts: ["verdict"], when: "verdict == yes" });
  g = updateEdge(g, "a", "c", { artifacts: ["notes"], when: "verdict == no", loop: { max: 2, carry: "notes" } });
  expect(lineOf(g, "a", "c")).toEqual([{ from: "a", to: "c", artifacts: ["notes"], when: "verdict == no", loop: { max: 2, carry: "notes" } }]);
  // the artifact the line no longer carries leaves the target's inputs; the new one joins
  expect(specOf(g, "c").inputs).toEqual(["notes"]);
  // absent when and loop clear them: the YAML entry goes back to a bare name
  g = updateEdge(g, "a", "c", { artifacts: ["notes"] });
  expect(lineOf(g, "a", "c")).toEqual([{ from: "a", to: "c", artifacts: ["notes"] }]);
  expect(toWorkflow(g).spec.edges.a).toEqual(["b", "c"]);
});

test("updateEdge with no artifacts is an ordering line; an input another line brings stays", () => {
  let g = connect(small(), "a", "b", { artifacts: ["notes"] });
  g = updateEdge(g, "a", "b", { artifacts: [] });
  expect(specOf(g, "b").inputs).toEqual([]);
  expect(lineOf(g, "a", "b")).toEqual([{ from: "a", to: "b", artifacts: [] }]);
  const both = updateEdge(connect(small(), "d", "b", { artifacts: ["notes"] }), "a", "b", { artifacts: [] });
  expect(specOf(both, "b").inputs).toEqual(["notes"]);
});

test("updateEdge refuses an artifact the source does not write, a loop without its max, and a missing line", () => {
  expect(() => updateEdge(small(), "a", "b", { artifacts: ["nope"] })).toThrow(/a writes no nope/);
  expect(() => updateEdge(small(), "a", "b", { artifacts: [], loop: { max: "" } })).toThrow(/needs its max/);
  expect(() => updateEdge(small(), "a", "c", { artifacts: [] })).toThrow(/no line/);
  expect(updateEdge(small(), "a", "b", { artifacts: ["notes"], loop: { max: "config.story.layer_attempts" } }).edges[0].loop).toEqual({ max: "config.story.layer_attempts" });
});
