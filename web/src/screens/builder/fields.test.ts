import RULE from "../../../../src/airlie/prompts/rules/output-format.md?raw";
import { fieldLines, formatSentence, kindText, OUTPUT_FORMAT_RULE, outputSpecs, parseFieldLines, parseKind, specValue } from "./fields";
import type { Kind } from "./fields";

const kind = (text: string): Kind => {
  const k = parseKind(text);
  if (k instanceof Error) throw k;
  return k;
};

test("the field kinds the server reads: scalars, lists of a kind, choices, a trailing ? for optional", () => {
  expect(kind("text")).toEqual({ name: "text", optional: false });
  expect(kind(" number? ")).toEqual({ name: "number", optional: true });
  expect(kind("list of bool")).toEqual({ name: "list", optional: false, item: { name: "bool", optional: false } });
  expect(kind("a | b")).toEqual({ name: "one_of", optional: false, options: ["a", "b"] });
  expect(kind("one of yes | no?")).toEqual({ name: "one_of", optional: true, options: ["yes", "no"] });
  expect(kind("one of solo")).toEqual({ name: "one_of", optional: false, options: ["solo"] });
});

test("a kind outside the grammar is an Error naming the text, as the server says it", () => {
  expect(String(parseKind("string"))).toContain("invalid field kind 'string': use text, number, bool, list of <kind>, or choices like 'a | b'");
  expect(String(parseKind("list of nothing"))).toContain("a list needs a kind of item");
  expect(parseKind("a | ")).toBeInstanceOf(Error);
});

test("the rule's text is the hub's output-format rule body", () => {
  expect(RULE.split("---\n").at(-1)!.trim()).toBe(OUTPUT_FORMAT_RULE);
});

test("formatSentence is the output-format rule's sentence for the fields, kinds as the server writes them", () => {
  expect(formatSentence({ verdict: "pass | fail", notes: "list of text?" })).toBe(
    "Reply with one ```json block with fields: verdict (one of pass | fail), notes (list of text, optional).",
  );
  expect(kindText(kind("list of a | b"))).toBe("list of one of a | b");
});

test("a fields text is one name: kind per line; a bad line says which", () => {
  expect(parseFieldLines("verdict: pass | fail\n\n notes : list of text?\n")).toEqual({ fields: { verdict: "pass | fail", notes: "list of text?" }, errors: [] });
  expect(parseFieldLines("verdict pass").errors).toEqual(["line 1: write name: kind"]);
  expect(parseFieldLines("a: text\nb: string").errors).toEqual([expect.stringMatching(/^b: invalid field kind 'string'/)]);
  expect(parseFieldLines("a: text\na: bool").errors).toEqual(["a is listed twice"]);
  expect(fieldLines({ verdict: "pass | fail", notes: "text" })).toBe("verdict: pass | fail\nnotes: text");
});

test("outputs read as specs whether bare or full, and write back bare when only a format", () => {
  expect(outputSpecs({ a: "json", b: { format: "json", fields: { x: "text" } }, c: { format: "bool", transient: true } })).toEqual({
    a: { format: "json" },
    b: { format: "json", fields: { x: "text" } },
    c: { format: "bool", transient: true },
  });
  expect(outputSpecs(undefined)).toEqual({});
  expect(specValue({ format: "text" })).toBe("text");
  expect(specValue({ format: "text", transient: false, fields: {} })).toBe("text");
  expect(specValue({ format: "bool", transient: true })).toEqual({ format: "bool", transient: true });
});
