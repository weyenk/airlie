import { holdGraph, takeGraph } from "./builder_state";
import { emptyGraph } from "./graph";

beforeEach(() => sessionStorage.clear());

test("a held canvas comes back once, with its prompts", () => {
  const graph = { ...emptyGraph("story"), outputs: ["x"] };
  const token = holdGraph("story", graph, { p: "text" });
  expect(takeGraph(token)).toEqual({ name: "story", graph, prompts: { p: "text" } });
  expect(takeGraph(token)).toBeNull();
});

test("two holds do not mix, and an unknown token is nothing", () => {
  const a = holdGraph("a", emptyGraph("a"), {});
  const b = holdGraph("b", emptyGraph("b"), {});
  expect(takeGraph(b)?.name).toBe("b");
  expect(takeGraph(a)?.name).toBe("a");
  expect(takeGraph("nope")).toBeNull();
});

test("a damaged entry holds nothing and does not throw", () => {
  sessionStorage.setItem("airlie.builderHold", "{not json");
  expect(takeGraph("x")).toBeNull();
  expect(takeGraph(holdGraph("a", emptyGraph("a"), {}))?.name).toBe("a");
});
