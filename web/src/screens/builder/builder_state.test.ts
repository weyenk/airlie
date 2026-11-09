import { holdGraph, takeGraph } from "./builder_state";
import { emptyGraph } from "./graph";

/** A hold that is known to have been stored. */
const hold = (...args: Parameters<typeof holdGraph>): string => holdGraph(...args)!;

beforeEach(() => sessionStorage.clear());

test("a held canvas comes back once, with its prompts", () => {
  const graph = { ...emptyGraph("story"), outputs: ["x"] };
  const token = hold("story", graph, { p: "text" });
  expect(takeGraph(token)).toEqual({ name: "story", graph, prompts: { p: "text" } });
  expect(takeGraph(token)).toBeNull();
});

test("two holds do not mix, and an unknown token is nothing", () => {
  const a = hold("a", emptyGraph("a"), {});
  const b = hold("b", emptyGraph("b"), {});
  expect(takeGraph(b)?.name).toBe("b");
  expect(takeGraph(a)?.name).toBe("a");
  expect(takeGraph("nope")).toBeNull();
});

test("a hold older than a day is dropped, on the next hold and on a take", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2026-11-09T10:00:00Z"));
    const old = hold("old", emptyGraph("old"), {});
    const old2 = hold("old2", emptyGraph("old2"), {});
    vi.setSystemTime(new Date("2026-11-10T10:00:01Z"));
    const fresh = hold("fresh", emptyGraph("fresh"), {}); // prunes `old` as it stores
    expect(Object.keys(JSON.parse(sessionStorage.getItem("airlie.builderHold")!))).toEqual([fresh]);
    expect(takeGraph(old)).toBeNull();
    expect(takeGraph(old2)).toBeNull();
    expect(takeGraph(fresh)?.name).toBe("fresh");
  } finally {
    vi.useRealTimers();
  }
});

test("only the newest ten holds are kept", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2026-11-09T10:00:00Z"));
    const tokens = Array.from({ length: 12 }, (_, i) => {
      vi.advanceTimersByTime(1000);
      return hold(`w${i}`, emptyGraph(`w${i}`), {});
    });
    expect(Object.keys(JSON.parse(sessionStorage.getItem("airlie.builderHold")!))).toHaveLength(10);
    expect(takeGraph(tokens[0])).toBeNull();
    expect(takeGraph(tokens[1])).toBeNull();
    expect(takeGraph(tokens[2])?.name).toBe("w2");
    expect(takeGraph(tokens[11])?.name).toBe("w11");
  } finally {
    vi.useRealTimers();
  }
});

test("a browser that will not store the hold gives no token", () => {
  const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota");
  });
  try {
    expect(holdGraph("a", emptyGraph("a"), {})).toBeNull();
  } finally {
    spy.mockRestore();
  }
});

test("a damaged entry holds nothing and does not throw", () => {
  sessionStorage.setItem("airlie.builderHold", "{not json");
  expect(takeGraph("x")).toBeNull();
  expect(takeGraph(hold("a", emptyGraph("a"), {}))?.name).toBe("a");
});
