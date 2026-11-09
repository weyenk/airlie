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

test("left to right: every line that is not a loop goes to a later column, 220 px apart", () => {
  const g = autoLayout(graphOf(STORY_YAML));
  for (const e of g.edges.filter((x) => !x.loop)) expect(at(g, e.from).x, `${e.from}>${e.to}`).toBeLessThan(at(g, e.to).x);
  expect(g.nodes.every((n) => n.x % 220 === 0 && n.y % 90 === 0)).toBe(true);
  expect(at(g, "intake")).toEqual({ x: 0, y: 0 });
  expect(at(g, "base")).toEqual({ x: 220, y: 0 });
});

test("a box's column is the longest run of lines that leads to it", () => {
  const g = autoLayout(graphOf(STORY_YAML));
  // to_build > gate > request > story_card: the request waits for the gate's column, though to_build lines to it too
  expect(at(g, "request").x).toBe(4 * 220);
  expect(at(g, "story_card").x).toBe(5 * 220);
  // build_layers sits after planned, the longest way in, not after base
  expect(at(g, "build_layers").x).toBe(at(g, "planned").x + 220);
  expect(at(g, "review").x).toBe(18 * 220);
});

test("boxes sharing a column stack 90 px apart in the file's order, and no two boxes share a place", () => {
  const g = autoLayout(graphOf(STORY_YAML));
  expect([at(g, "take_card_as_story"), at(g, "refine"), at(g, "survey")]).toEqual([
    { x: 1320, y: 0 },
    { x: 1320, y: 90 },
    { x: 1320, y: 180 },
  ]);
  expect(at(g, "agree_plain").x).toBe(at(g, "amigos").x);
  expect(new Set(g.nodes.map((n) => `${n.x},${n.y}`)).size).toBe(g.nodes.length);
});

test("a loop line does not push its target right", () => {
  const g = autoLayout(graphOf(LAYER_YAML));
  expect(at(g, "package").x).toBeLessThan(at(g, "build").x);
  expect(at(g, "start")).toEqual({ x: 0, y: 0 });
});

test("boxes with a place keep it; only the others are placed", () => {
  const g = autoLayout(graphOf(STORY_YAML, { base: { x: 500, y: 333 } }));
  expect(at(g, "base")).toEqual({ x: 500, y: 333 });
  expect(at(g, "to_build")).toEqual({ x: 440, y: 0 });
});

test("it returns a new graph", () => {
  const g = graphOf(STORY_YAML);
  const before = JSON.stringify(g);
  autoLayout(g);
  expect(JSON.stringify(g)).toBe(before);
});

test("a box laid out automatically never lands on a placed one", () => {
  const g = autoLayout(graphOf("workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a}\n      b: {type: check, run: x.b}\n", { b: { x: 0, y: 0 } }));
  expect(at(g, "b")).toEqual({ x: 0, y: 0 });
  expect(at(g, "a")).not.toEqual({ x: 0, y: 0 });
});

test("a cycle of lines without a loop bound still places every box", () => {
  const text = "workflows:\n  w:\n    nodes:\n      a: {type: check, run: x.a, to: [b]}\n      b: {type: check, run: x.b, to: [a]}\n";
  const g = autoLayout(graphOf(text));
  expect(g.nodes.every((n) => n.placed)).toBe(true);
  expect(at(g, "a")).not.toEqual(at(g, "b"));
});
