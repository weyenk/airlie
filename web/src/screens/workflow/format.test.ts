import { briefSpan, count, span, stats } from "./format";
import { job } from "@/test-utils/workflow-fixtures";

test("span reads seconds, minutes and hours", () => {
  expect(span(3)).toBe("3 s");
  expect(span(217)).toBe("3 min 37 s");
  expect(span(3700)).toBe("1 h 1 min");
  expect(span(null)).toBe("");
});

test("briefSpan is the short form a box shows", () => {
  expect(briefSpan(28)).toBe("28 s");
  expect(briefSpan(59.6)).toBe("1 min");
  expect(briefSpan(125)).toBe("2 min");
  expect(briefSpan(3700)).toBe("1 h 1 min");
});

test("count shortens thousands", () => {
  expect(count(950)).toBe("950");
  expect(count(48200)).toBe("48.2k");
});

test("stats list only the numbers a job has", () => {
  expect(stats(job({ duration_s: null, attempts: 0 }))).toEqual([["Attempts", "0"]]);
  const rows = stats(
    job({
      fix_rounds: 2,
      tokens: { steps: 14, prompt: 48200, completion: 3100, gen_s: 1, prefill_s: 1, tool_s: 1, tps: 38.2, effective_tps: 21, prefill_tps: null, role: "coder", model: "qwen" },
    }),
  );
  expect(rows).toEqual([
    ["Attempts", "1"],
    ["Fix rounds", "2"],
    ["Duration", "2 min 5 s"],
    ["Steps", "14"],
    ["Prompt tokens", "48.2k"],
    ["Completion tokens", "3.1k"],
    ["Write speed", "38.2 tok/s"],
    ["End to end", "21 tok/s"],
    ["Model", "qwen"],
  ]);
});

test("a run's job has only prompt and completion token counts: the other numbers are left out, not shown as undefined", () => {
  const partial = { prompt: 1200, completion: 90 } as unknown as NonNullable<ReturnType<typeof job>["tokens"]>;
  expect(stats(job({ tokens: partial, duration_s: null }))).toEqual([
    ["Attempts", "1"],
    ["Prompt tokens", "1.2k"],
    ["Completion tokens", "90"],
  ]);
});
