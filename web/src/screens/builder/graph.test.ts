import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { fromWorkflow } from "./graph";
import type { GEdge, Graph } from "./graph";
import { parseWorkflow } from "./yaml";

const graphOf = (text: string): Graph => {
  const { name, spec, layout } = parseWorkflow(text);
  return fromWorkflow(name, spec, layout);
};
const edgesOf = (g: Graph, kind: GEdge["kind"]) => g.edges.filter((e) => e.kind === kind).map((e) => `${e.from}>${e.to}`);
const outOf = (g: Graph, id: string) => g.edges.filter((e) => e.from === id);

test("the shipped story workflow: a node per entry, in the file's order, with its type, start and loose names", () => {
  const g = graphOf(STORY_YAML);
  expect(g.name).toBe("story");
  expect(g.type).toBe("story");
  expect(g.start).toBe("intake");
  expect(g.loose).toEqual(["card", "repo"]);
  // every entry under nodes: (the plan said 17; the shipped file has 19, survey and refine counted)
  expect(g.nodes.map((n) => n.id)).toEqual([
    "intake", "base", "gate", "take_card_as_story", "plan_story", "survey", "refine", "costed_refine", "layers", "costed_plan",
    "agree_plain", "amigos", "costed_amigos", "planned", "build_layers", "built", "publish", "costed_publish", "review",
  ]);
  expect(g.nodes.find((n) => n.id === "base")).toMatchObject({ type: "check", spec: { run: "story.base", outputs: { base: "json" } } });
  expect(g.nodes.find((n) => n.id === "base")?.spec).not.toHaveProperty("type");
});

test("plan_story fans out to refine and survey, which both rejoin at costed_refine; nothing else wires to them", () => {
  const g = graphOf(STORY_YAML);
  expect(edgesOf(g, "parallel")).toEqual(["plan_story>refine", "plan_story>survey", "refine>costed_refine", "survey>costed_refine"]);
  expect(outOf(g, "plan_story").every((e) => e.kind === "parallel")).toBe(true);
  expect(g.edges.filter((e) => e.kind !== "parallel" && ["refine", "survey"].includes(e.to))).toEqual([]);
  expect(edgesOf(g, "sequence")).toContain("take_card_as_story>plan_story");
});

test("the sequence follows the file's order, the last node going nowhere", () => {
  const g = graphOf(STORY_YAML);
  expect(edgesOf(g, "sequence")).toEqual([
    "intake>base", "base>gate", "gate>take_card_as_story", "take_card_as_story>plan_story", "costed_refine>layers", "layers>costed_plan",
    "costed_plan>agree_plain", "agree_plain>amigos", "amigos>costed_amigos", "costed_amigos>planned", "planned>build_layers",
    "build_layers>built", "built>publish", "publish>costed_publish", "costed_publish>review",
  ]);
  expect(outOf(g, "review")).toEqual([]);
});

test("build_layers is one each node naming its bundle", () => {
  const n = graphOf(STORY_YAML).nodes.find((x) => x.id === "build_layers");
  expect(n).toMatchObject({ type: "each", spec: { over: "layers", workflow: "layer" } });
});

test("a loop is a loop wire back to its target carrying its max and carry, and the sequence still goes on", () => {
  const g = graphOf(LAYER_YAML);
  expect(g.type).toBeNull();
  expect(g.edges.filter((e) => e.kind === "loop")).toEqual([{ from: "build", to: "package", kind: "loop", loop: { max: "story.layer_attempts", carry: "feedback" } }]);
  expect(edgesOf(g, "sequence")).toContain("build>costed_layer");
});

const SMALL = `workflows:
  w:
    start: a
    nodes:
      a:
        type: check
        run: x.a
        on: verdict
        branch: {yes: c, no: d}
      b:
        type: check
        run: x.b
      c:
        type: check
        run: x.c
        next: e
        on_fail: {to: a, max: 2, when: x.retry}
      d:
        type: check
        run: x.d
        on: verdict
        branch: {"*": e}
      e:
        type: gate
        run: x.e
`;

test("a branch is a wire per value, labelled; without a * default the node still falls through", () => {
  const g = graphOf(SMALL);
  expect(g.edges.filter((e) => e.kind === "branch")).toEqual([
    { from: "a", to: "c", kind: "branch", label: "yes" },
    { from: "a", to: "d", kind: "branch", label: "no" },
    { from: "d", to: "e", kind: "branch", label: "*" },
  ]);
  expect(edgesOf(g, "sequence")).toEqual(["a>b", "b>c", "c>e"]); // d's * default leaves no fall-through; c's next wins
});

test("an on_fail loop is a loop wire too", () => {
  const g = graphOf(SMALL);
  expect(g.edges.filter((e) => e.kind === "loop")).toEqual([{ from: "c", to: "a", kind: "loop", loop: { max: 2, when: "x.retry" } }]);
});

test("a given layout places the boxes", () => {
  const { name, spec } = parseWorkflow(SMALL);
  const g = fromWorkflow(name, spec, { b: { x: 300, y: 40 } });
  expect(g.nodes.find((n) => n.id === "b")).toMatchObject({ x: 300, y: 40 });
});
