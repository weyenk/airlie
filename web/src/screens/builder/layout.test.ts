import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { fromWorkflow } from "./graph";
import type { Graph } from "./graph";
import { autoLayout } from "./layout";
import { parseWorkflow } from "./yaml";

const graphOf = (text: string, layout?: Record<string, { x: number; y: number }>): Graph => {
  const parsed = parseWorkflow(text);
  return fromWorkflow(parsed.name, parsed.spec, layout ?? parsed.layout);
};
const at = (g: Graph, id: string) => {
  const n = g.nodes.find((x) => x.id === id)!;
  return { x: n.x, y: n.y };
};

test("left to right: every sequence and parallel wire goes to a later column, 160 px apart", () => {
  const g = autoLayout(graphOf(STORY_YAML));
  for (const e of g.edges.filter((x) => x.kind !== "loop")) expect(at(g, e.from).x, `${e.from}>${e.to}`).toBeLessThan(at(g, e.to).x);
  expect(g.nodes.every((n) => n.x % 160 === 0 && n.y % 90 === 0)).toBe(true);
  expect(at(g, "intake")).toEqual({ x: 0, y: 0 });
  expect(at(g, "base")).toEqual({ x: 160, y: 0 });
});

test("the sequence's x only grows, and no two boxes share a place", () => {
  const g = autoLayout(graphOf(STORY_YAML));
  const chain = g.nodes.filter((n) => !["refine", "survey"].includes(n.id)).map((n) => n.x);
  expect(chain).toEqual([...chain].sort((a, b) => a - b));
  expect(new Set(chain).size).toBe(chain.length);
  expect(new Set(g.nodes.map((n) => `${n.x},${n.y}`)).size).toBe(g.nodes.length);
});

test("parallel children stack in one column, 90 px apart, between the parallel node and what follows it", () => {
  const g = autoLayout(graphOf(STORY_YAML));
  const survey = at(g, "survey");
  const refine = at(g, "refine");
  expect(survey.x).toBe(refine.x);
  expect(Math.abs(survey.y - refine.y)).toBe(90);
  expect(survey.x).toBe(at(g, "plan_story").x + 160);
  expect(at(g, "costed_refine").x).toBe(survey.x + 160);
});

test("a loop wire does not push its target right", () => {
  const g = autoLayout(graphOf(LAYER_YAML));
  expect(at(g, "package").x).toBeLessThan(at(g, "build").x);
});

test("boxes with a place keep it; only the others are placed", () => {
  const g = autoLayout(graphOf(STORY_YAML, { base: { x: 500, y: 333 } }));
  expect(at(g, "base")).toEqual({ x: 500, y: 333 });
  expect(at(g, "gate")).toEqual({ x: 320, y: 0 });
});

test("it returns a new graph", () => {
  const g = graphOf(STORY_YAML);
  const before = JSON.stringify(g);
  autoLayout(g);
  expect(JSON.stringify(g)).toBe(before);
});

test("a box laid out automatically never lands on a placed one", () => {
  const g = autoLayout(graphOf("workflows:\n  w:\n    start: a\n    nodes:\n      a: {type: check, run: x.a}\n      b: {type: check, run: x.b}\n", { b: { x: 0, y: 0 } }));
  expect(at(g, "b")).toEqual({ x: 0, y: 0 });
  expect(at(g, "a")).not.toEqual({ x: 0, y: 0 });
});
